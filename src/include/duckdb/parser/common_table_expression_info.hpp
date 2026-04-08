//===----------------------------------------------------------------------===//
//                         DuckDB
//
// duckdb/parser/common_table_expression_info.hpp
//
//
//===----------------------------------------------------------------------===//

#pragma once

#include "duckdb/common/vector.hpp"
#include "duckdb/common/enums/cte_materialize.hpp"

namespace duckdb {

class SelectStatement;

struct CommonTableExpressionInfo {
	~CommonTableExpressionInfo();

	vector<string> aliases;
	vector<unique_ptr<ParsedExpression>> key_targets;
	unique_ptr<SelectStatement> query;
	CTEMaterialize materialized = CTEMaterialize::CTE_MATERIALIZE_DEFAULT;
	bool has_hidden_rowid = false;
	string hidden_rowid_name;

public:
	void Serialize(Serializer &serializer) const;
	static unique_ptr<CommonTableExpressionInfo> Deserialize(Deserializer &deserializer);
	unique_ptr<CommonTableExpressionInfo> Copy();

private:
	CTEMaterialize GetMaterializedForSerialization(Serializer &serializer) const;
};

} // namespace duckdb
