#include "duckdb/storage/string_uncompressed.hpp"

#include "duckdb/common/pair.hpp"
#include "duckdb/common/serializer/deserializer.hpp"
#include "duckdb/common/serializer/serializer.hpp"
#include "duckdb/storage/checkpoint/write_overflow_strings_to_disk.hpp"
#include "duckdb/storage/table/column_data.hpp"

namespace duckdb {

//===--------------------------------------------------------------------===//
// Storage Class
//===--------------------------------------------------------------------===//
UncompressedStringSegmentState::~UncompressedStringSegmentState() {
	while (head) {
		// prevent deep recursion here
		head = std::move(head->next);
	}
}

//===--------------------------------------------------------------------===//
// Analyze
//===--------------------------------------------------------------------===//
struct StringAnalyzeState : public AnalyzeState {
	explicit StringAnalyzeState(const CompressionInfo &info)
	    : AnalyzeState(info), count(0), total_string_size(0), overflow_strings(0) {
	}

	idx_t count;
	idx_t total_string_size;
	idx_t overflow_strings;
};

unique_ptr<AnalyzeState> UncompressedStringStorage::StringInitAnalyze(ColumnData &col_data, PhysicalType type) {
	CompressionInfo info(col_data.GetBlockManager().GetBlockSize());
	return make_uniq<StringAnalyzeState>(info);
}

bool UncompressedStringStorage::StringAnalyze(AnalyzeState &state_p, Vector &input, idx_t count) {
	auto &state = state_p.Cast<StringAnalyzeState>();
	UnifiedVectorFormat vdata;
	input.ToUnifiedFormat(count, vdata);

	state.count += count;
	auto data = UnifiedVectorFormat::GetData<string_t>(vdata);
	for (idx_t i = 0; i < count; i++) {
		auto idx = vdata.sel->get_index(i);
		if (vdata.validity.RowIsValid(idx)) {
			auto string_size = data[idx].GetSize();
			state.total_string_size += string_size;
			if (string_size >= StringUncompressed::GetStringBlockLimit(state.info.GetBlockSize())) {
				state.overflow_strings++;
			}
		}
	}
	return true;
}

idx_t UncompressedStringStorage::StringFinalAnalyze(AnalyzeState &state_p) {
	auto &state = state_p.Cast<StringAnalyzeState>();
	return state.count * sizeof(int32_t) + state.total_string_size + state.overflow_strings * BIG_STRING_MARKER_SIZE;
}

//===--------------------------------------------------------------------===//
// Scan
//===--------------------------------------------------------------------===//
void UncompressedStringInitPrefetch(ColumnSegment &segment, PrefetchState &prefetch_state) {
	prefetch_state.AddBlock(segment.block);
	auto segment_state = segment.GetSegmentState();
	if (segment_state) {
		auto &state = segment_state->Cast<UncompressedStringSegmentState>();
		auto &block_manager = segment.GetBlockManager();
		for (auto &block_id : state.on_disk_blocks) {
			auto block_handle = state.GetHandle(block_manager, block_id);
			prefetch_state.AddBlock(block_handle);
		}
	}
}

unique_ptr<SegmentScanState> UncompressedStringStorage::StringInitScan(ColumnSegment &segment) {
	auto result = make_uniq<StringScanState>();
	auto &buffer_manager = BufferManager::GetBufferManager(segment.db);
	result->handle = buffer_manager.Pin(segment.block);
	return std::move(result);
}

//===--------------------------------------------------------------------===//
// Scan base data
//===--------------------------------------------------------------------===//
void UncompressedStringStorage::StringScanPartial(ColumnSegment &segment, ColumnScanState &state, idx_t scan_count,
                                                  Vector &result, idx_t result_offset) {
	// clear any previously locked buffers and get the primary buffer handle
	auto &scan_state = state.scan_state->Cast<StringScanState>();
	auto start = segment.GetRelativeIndex(state.row_index);

	auto baseptr = scan_state.handle.Ptr() + segment.GetBlockOffset();
	auto dict_end = GetDictionaryEnd(segment, scan_state.handle);
	auto base_data = reinterpret_cast<int32_t *>(baseptr + DICTIONARY_HEADER_SIZE);
	auto result_data = FlatVector::GetData<string_t>(result);

	int32_t previous_offset = start > 0 ? base_data[start - 1] : 0;

	for (idx_t i = 0; i < scan_count; i++) {
		// std::abs used since offsets can be negative to indicate big strings
		auto current_offset = base_data[start + i];
		auto string_length = UnsafeNumericCast<uint32_t>(std::abs(current_offset) - std::abs(previous_offset));
		result_data[result_offset + i] =
		    FetchStringFromDict(segment, dict_end, result, baseptr, current_offset, string_length);
		previous_offset = base_data[start + i];
	}
}

void UncompressedStringStorage::StringScan(ColumnSegment &segment, ColumnScanState &state, idx_t scan_count,
                                           Vector &result) {
	StringScanPartial(segment, state, scan_count, result, 0);
}

//===--------------------------------------------------------------------===//
// Select
//===--------------------------------------------------------------------===//
void UncompressedStringStorage::Select(ColumnSegment &segment, ColumnScanState &state, idx_t vector_count,
                                       Vector &result, const SelectionVector &sel, idx_t sel_count) {
	// clear any previously locked buffers and get the primary buffer handle
	auto &scan_state = state.scan_state->Cast<StringScanState>();
	auto start = segment.GetRelativeIndex(state.row_index);

	auto baseptr = scan_state.handle.Ptr() + segment.GetBlockOffset();
	auto dict_end = GetDictionaryEnd(segment, scan_state.handle);
	auto base_data = reinterpret_cast<int32_t *>(baseptr + DICTIONARY_HEADER_SIZE);
	auto result_data = FlatVector::GetData<string_t>(result);

	for (idx_t i = 0; i < sel_count; i++) {
		idx_t index = start + sel.get_index(i);
		auto current_offset = base_data[index];
		auto prev_offset = index > 0 ? base_data[index - 1] : 0;
		auto string_length = UnsafeNumericCast<uint32_t>(std::abs(current_offset) - std::abs(prev_offset));
		result_data[i] = FetchStringFromDict(segment, dict_end, result, baseptr, current_offset, string_length);
	}
}

//===--------------------------------------------------------------------===//
// Fetch
//===--------------------------------------------------------------------===//
BufferHandle &ColumnFetchState::GetOrInsertHandle(ColumnSegment &segment) {
	auto primary_id = segment.block->BlockId();

	auto entry = handles.find(primary_id);
	if (entry == handles.end()) {
		// not pinned yet: pin it
		auto &buffer_manager = BufferManager::GetBufferManager(segment.db);
		auto handle = buffer_manager.Pin(segment.block);
		auto pinned_entry = handles.insert(make_pair(primary_id, std::move(handle)));
		return pinned_entry.first->second;
	} else {
		// already pinned: use the pinned handle
		return entry->second;
	}
}

void UncompressedStringStorage::StringFetchRow(ColumnSegment &segment, ColumnFetchState &state, row_t row_id,
                                               Vector &result, idx_t result_idx) {
	// fetch a single row from the string segment
	// first pin the main buffer if it is not already pinned
	auto &handle = state.GetOrInsertHandle(segment);

	auto baseptr = handle.Ptr() + segment.GetBlockOffset();
	auto dict_end = GetDictionaryEnd(segment, handle);
	auto base_data = reinterpret_cast<int32_t *>(baseptr + DICTIONARY_HEADER_SIZE);
	auto result_data = FlatVector::GetData<string_t>(result);

	auto dict_offset = base_data[row_id];
	uint32_t string_length;
	if (DUCKDB_UNLIKELY(row_id == 0LL)) {
		// edge case where this is the first string in the dict
		string_length = NumericCast<uint32_t>(std::abs(dict_offset));
	} else {
		string_length = NumericCast<uint32_t>(std::abs(dict_offset) - std::abs(base_data[row_id - 1]));
	}
	result_data[result_idx] = FetchStringFromDict(segment, dict_end, result, baseptr, dict_offset, string_length);
}

//===--------------------------------------------------------------------===//
// Append
//===--------------------------------------------------------------------===//
SerializedStringSegmentState::SerializedStringSegmentState() {
}

SerializedStringSegmentState::SerializedStringSegmentState(vector<block_id_t> blocks_p) {
	blocks = std::move(blocks_p);
}

void SerializedStringSegmentState::Serialize(Serializer &serializer) const {
	serializer.WriteProperty(1, "overflow_blocks", blocks);
}

unique_ptr<CompressedSegmentState>
UncompressedStringStorage::StringInitSegment(ColumnSegment &segment, block_id_t block_id,
                                             optional_ptr<ColumnSegmentState> segment_state) {
	auto &buffer_manager = BufferManager::GetBufferManager(segment.db);
	if (block_id == INVALID_BLOCK) {
		auto handle = buffer_manager.Pin(segment.block);
		StringDictionaryContainer dictionary;
		dictionary.size = 0;
		dictionary.end = UnsafeNumericCast<uint32_t>(segment.SegmentSize());
		SetDictionary(segment, handle, dictionary);
	}
	auto result = make_uniq<UncompressedStringSegmentState>();
	if (segment_state) {
		auto &serialized_state = segment_state->Cast<SerializedStringSegmentState>();
		result->on_disk_blocks = std::move(serialized_state.blocks);
	}
	return std::move(result);
}

idx_t UncompressedStringStorage::FinalizeAppend(ColumnSegment &segment, SegmentStatistics &) {
	auto &buffer_manager = BufferManager::GetBufferManager(segment.db);
	auto handle = buffer_manager.Pin(segment.block);
	auto dict = GetDictionary(segment, handle);
	D_ASSERT(dict.end == segment.SegmentSize());
	// compute the total size required to store this segment
	auto offset_size = DICTIONARY_HEADER_SIZE + segment.count * sizeof(int32_t);
	auto total_size = offset_size + dict.size;

	CompressionInfo info(segment.GetBlockManager().GetBlockSize());
	if (total_size >= info.GetCompactionFlushLimit()) {
		// the block is full enough, don't bother moving around the dictionary
		return segment.SegmentSize();
	}

	// the block has space left: figure out how much space we can save
	auto move_amount = segment.SegmentSize() - total_size;
	// move the dictionary so it lines up exactly with the offsets
	auto dataptr = handle.Ptr();
	memmove(dataptr + offset_size, dataptr + dict.end - dict.size, dict.size);
	dict.end -= move_amount;
	D_ASSERT(dict.end == total_size);
	// write the new dictionary (with the updated "end")
	SetDictionary(segment, handle, dict);
	return total_size;
}

//===--------------------------------------------------------------------===//
// Serialization & Cleanup
//===--------------------------------------------------------------------===//
unique_ptr<ColumnSegmentState> UncompressedStringStorage::SerializeState(ColumnSegment &segment) {
	auto &state = segment.GetSegmentState()->Cast<UncompressedStringSegmentState>();
	if (state.on_disk_blocks.empty()) {
		// no on-disk blocks - nothing to write
		return nullptr;
	}
	return make_uniq<SerializedStringSegmentState>(state.on_disk_blocks);
}

unique_ptr<ColumnSegmentState> UncompressedStringStorage::DeserializeState(Deserializer &deserializer) {
	auto result = make_uniq<SerializedStringSegmentState>();
	deserializer.ReadProperty(1, "overflow_blocks", result->blocks);
	return std::move(result);
}

void UncompressedStringStorage::CleanupState(ColumnSegment &segment) {
	auto &state = segment.GetSegmentState()->Cast<UncompressedStringSegmentState>();
	auto &block_manager = segment.GetBlockManager();
	state.Cleanup(block_manager);
}

//===--------------------------------------------------------------------===//
// Get Function
//===--------------------------------------------------------------------===//
CompressionFunction StringUncompressed::GetFunction(PhysicalType data_type) {
	D_ASSERT(data_type == PhysicalType::VARCHAR);
	return CompressionFunction(
	    CompressionType::COMPRESSION_UNCOMPRESSED, data_type, UncompressedStringStorage::StringInitAnalyze,
	    UncompressedStringStorage::StringAnalyze, UncompressedStringStorage::StringFinalAnalyze,
	    UncompressedFunctions::InitCompression, UncompressedFunctions::Compress,
	    UncompressedFunctions::FinalizeCompress, UncompressedStringStorage::StringInitScan,
	    UncompressedStringStorage::StringScan, UncompressedStringStorage::StringScanPartial,
	    UncompressedStringStorage::StringFetchRow, UncompressedFunctions::EmptySkip,
	    UncompressedStringStorage::StringInitSegment, UncompressedStringStorage::StringInitAppend,
	    UncompressedStringStorage::StringAppend, UncompressedStringStorage::FinalizeAppend, nullptr,
	    UncompressedStringStorage::SerializeState, UncompressedStringStorage::DeserializeState,
	    UncompressedStringStorage::CleanupState, UncompressedStringInitPrefetch, UncompressedStringStorage::Select);
}

//===--------------------------------------------------------------------===//
// Helper Functions
//===--------------------------------------------------------------------===//
void UncompressedStringStorage::SetDictionary(ColumnSegment &segment, BufferHandle &handle,
                                              StringDictionaryContainer container) {
	auto startptr = handle.Ptr() + segment.GetBlockOffset();
	Store<uint32_t>(container.size, startptr);
	Store<uint32_t>(container.end, startptr + sizeof(uint32_t));
}

StringDictionaryContainer UncompressedStringStorage::GetDictionary(ColumnSegment &segment, BufferHandle &handle) {
	auto startptr = handle.Ptr() + segment.GetBlockOffset();
	StringDictionaryContainer container;
	container.size = Load<uint32_t>(startptr);
	container.end = Load<uint32_t>(startptr + sizeof(uint32_t));
	return container;
}

uint32_t UncompressedStringStorage::GetDictionaryEnd(ColumnSegment &segment, BufferHandle &handle) {
	auto startptr = handle.Ptr() + segment.GetBlockOffset();
	return Load<uint32_t>(startptr + sizeof(uint32_t));
}

idx_t UncompressedStringStorage::RemainingSpace(ColumnSegment &segment, BufferHandle &handle) {
	auto dictionary = GetDictionary(segment, handle);
	D_ASSERT(dictionary.end == segment.SegmentSize());
	idx_t used_space = dictionary.size + segment.count * sizeof(int32_t) + DICTIONARY_HEADER_SIZE;
	D_ASSERT(segment.SegmentSize() >= used_space);
	return segment.SegmentSize() - used_space;
}

void UncompressedStringStorage::WriteString(ColumnSegment &segment, string_t string, block_id_t &result_block,
                                            int32_t &result_offset) {
	auto &state = segment.GetSegmentState()->Cast<UncompressedStringSegmentState>();
	if (state.overflow_writer) {
		// overflow writer is set: write string there
		state.overflow_writer->WriteString(state, string, result_block, result_offset);
	} else {
		// default overflow behavior: use in-memory buffer to store the overflow string
		WriteStringMemory(segment, string, result_block, result_offset);
	}
}

void UncompressedStringStorage::WriteStringMemory(ColumnSegment &segment, string_t string, block_id_t &result_block,
                                                  int32_t &result_offset) {
	auto total_length = UnsafeNumericCast<uint32_t>(string.GetSize() + sizeof(uint32_t));
	shared_ptr<BlockHandle> block;
	BufferHandle handle;

	auto &buffer_manager = BufferManager::GetBufferManager(segment.db);
	auto &state = segment.GetSegmentState()->Cast<UncompressedStringSegmentState>();
	// check if the string fits in the current block
	if (!state.head || state.head->offset + total_length >= state.head->size) {
		// string does not fit, allocate space for it
		// create a new string block
		auto alloc_size = MaxValue<idx_t>(total_length, segment.GetBlockManager().GetBlockSize());
		auto new_block = make_uniq<StringBlock>();
		new_block->offset = 0;
		new_block->size = alloc_size;
		// allocate an in-memory buffer for it
		handle = buffer_manager.Allocate(MemoryTag::OVERFLOW_STRINGS, alloc_size, false);
		block = handle.GetBlockHandle();
		state.overflow_blocks.insert(make_pair(block->BlockId(), reference<StringBlock>(*new_block)));
		new_block->block = std::move(block);
		new_block->next = std::move(state.head);
		state.head = std::move(new_block);
	} else {
		// string fits, copy it into the current block
		handle = buffer_manager.Pin(state.head->block);
	}

	result_block = state.head->block->BlockId();
	result_offset = UnsafeNumericCast<int32_t>(state.head->offset);

	// copy the string and the length there
	auto ptr = handle.Ptr() + state.head->offset;
	Store<uint32_t>(UnsafeNumericCast<uint32_t>(string.GetSize()), ptr);
	ptr += sizeof(uint32_t);
	memcpy(ptr, string.GetData(), string.GetSize());
	state.head->offset += total_length;
}

string_t UncompressedStringStorage::ReadOverflowString(ColumnSegment &segment, Vector &result, block_id_t block,
                                                       int32_t offset) {
	auto &block_manager = segment.GetBlockManager();
	auto &buffer_manager = block_manager.buffer_manager;
	auto &state = segment.GetSegmentState()->Cast<UncompressedStringSegmentState>();

	D_ASSERT(block != INVALID_BLOCK);
	D_ASSERT(offset < NumericCast<int32_t>(block_manager.GetBlockSize()));

	if (block < MAXIMUM_BLOCK) {
		// read the overflow string from disk
		// pin the initial handle and read the length
		auto block_handle = state.GetHandle(block_manager, block);
		auto handle = buffer_manager.Pin(block_handle);

		// read header
		uint32_t length = Load<uint32_t>(handle.Ptr() + offset);
		uint32_t remaining = length;
		offset += sizeof(uint32_t);

		BufferHandle target_handle;
		string_t overflow_string;
		data_ptr_t target_ptr;
		bool allocate_block = length >= block_manager.GetBlockSize();
		if (allocate_block) {
			// overflow string is bigger than a block - allocate a temporary buffer for it
			target_handle = buffer_manager.Allocate(MemoryTag::OVERFLOW_STRINGS, length);
			target_ptr = target_handle.Ptr();
		} else {
			// overflow string is smaller than a block - add it to the vector directly
			overflow_string = StringVector::EmptyString(result, length);
			target_ptr = data_ptr_cast(overflow_string.GetDataWriteable());
		}

		// now append the string to the single buffer
		while (remaining > 0) {
			idx_t to_write = MinValue<idx_t>(remaining, block_manager.GetBlockSize() - sizeof(block_id_t) -
			                                                UnsafeNumericCast<idx_t>(offset));
			memcpy(target_ptr, handle.Ptr() + offset, to_write);
			remaining -= to_write;
			offset += UnsafeNumericCast<int32_t>(to_write);
			target_ptr += to_write;
			if (remaining > 0) {
				// read the next block
				block_id_t next_block = Load<block_id_t>(handle.Ptr() + offset);
				block_handle = state.GetHandle(block_manager, next_block);
				handle = buffer_manager.Pin(block_handle);
				offset = 0;
			}
		}
		if (allocate_block) {
			auto final_buffer = target_handle.Ptr();
			StringVector::AddHandle(result, std::move(target_handle));
			return ReadString(final_buffer, 0, length);
		} else {
			overflow_string.Finalize();
			return overflow_string;
		}
	}

	// read the overflow string from memory
	// first pin the handle, if it is not pinned yet
	auto entry = state.overflow_blocks.find(block);
	D_ASSERT(entry != state.overflow_blocks.end());
	auto handle = buffer_manager.Pin(entry->second.get().block);
	auto final_buffer = handle.Ptr();
	StringVector::AddHandle(result, std::move(handle));
	return ReadStringWithLength(final_buffer, offset);
}

string_t UncompressedStringStorage::ReadString(data_ptr_t target, int32_t offset, uint32_t string_length) {
	auto ptr = target + offset;
	auto str_ptr = char_ptr_cast(ptr);
	return string_t(str_ptr, string_length);
}

string_t UncompressedStringStorage::ReadStringWithLength(data_ptr_t target, int32_t offset) {
	auto ptr = target + offset;
	auto str_length = Load<uint32_t>(ptr);
	auto str_ptr = char_ptr_cast(ptr + sizeof(uint32_t));
	return string_t(str_ptr, str_length);
}

void UncompressedStringStorage::WriteStringMarker(data_ptr_t target, block_id_t block_id, int32_t offset) {
	memcpy(target, &block_id, sizeof(block_id_t));
	target += sizeof(block_id_t);
	memcpy(target, &offset, sizeof(int32_t));
}

void UncompressedStringStorage::ReadStringMarker(data_ptr_t target, block_id_t &block_id, int32_t &offset) {
	memcpy(&block_id, target, sizeof(block_id_t));
	target += sizeof(block_id_t);
	memcpy(&offset, target, sizeof(int32_t));
}

} // namespace duckdb
