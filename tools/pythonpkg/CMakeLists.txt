include_directories(src)
include_directories(../../)
include_directories(src/include)

find_package(PythonLibs)
if(NOT PythonLibs_FOUND)
  return()
endif()

find_package(pybind11)
find_package(pybind11 PATHS /usr/local/lib/python3.8/site-packages/pybind11/)
if(NOT pybind11_FOUND)
  error("fix for clion")
  return()
endif()

include_directories(${pybind11_INCLUDE_DIR})
include_directories(${PYTHON_INCLUDE_DIRS})

add_subdirectory(src)
# this is used for clang-tidy checks

set(ALL_OBJECT_FILES duckdb_python.cpp ${ALL_OBJECT_FILES})

add_library(python_duckdb ${ALL_OBJECT_FILES})
target_include_directories(python_duckdb PRIVATE ${PYTHON_INCLUDE_DIRS})
target_link_libraries(python_duckdb duckdb_static ${PYTHON_LIBRARIES})
