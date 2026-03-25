# simple DB API testcase

import duckdb
import pandas as pd
import pytest
from conftest import NumpyPandas, ArrowPandas
from packaging.version import Version

numpy_nullable_df = pd.DataFrame([{"COL1": "val1", "CoL2": 1.05}, {"COL1": "val4", "CoL2": 17}])

try:
    from pandas.compat import pa_version_under7p0

    pyarrow_dtypes_enabled = not pa_version_under7p0
except:
    pyarrow_dtypes_enabled = False

if Version(pd.__version__) >= Version('2.0.0') and pyarrow_dtypes_enabled:
    pyarrow_df = numpy_nullable_df.convert_dtypes(dtype_backend="pyarrow")
else:
    # dtype_backend is not supported in pandas < 2.0.0
    pyarrow_df = numpy_nullable_df


class TestImplicitPandasScan(object):
    @pytest.mark.parametrize('pandas', [NumpyPandas(), ArrowPandas()])
    def test_local_pandas_scan(self, duckdb_cursor, pandas):
        con = duckdb.connect()
        df = pandas.DataFrame([{"COL1": "val1", "CoL2": 1.05}, {"COL1": "val3", "CoL2": 17}])
        r1 = con.execute('select * from df').fetchdf()
        assert r1["COL1"][0] == "val1"
        assert r1["COL1"][1] == "val3"
        assert r1["CoL2"][0] == 1.05
        assert r1["CoL2"][1] == 17

    @pytest.mark.parametrize('pandas', [NumpyPandas(), ArrowPandas()])
    def test_global_pandas_scan(self, duckdb_cursor, pandas):
        con = duckdb.connect()
        r1 = con.execute(f'select * from {pandas.backend}_df').fetchdf()
        assert r1["COL1"][0] == "val1"
        assert r1["COL1"][1] == "val4"
        assert r1["CoL2"][0] == 1.05
        assert r1["CoL2"][1] == 17

    @pytest.mark.parametrize('pandas', [NumpyPandas(), ArrowPandas()])
    def test_rowid_hidden_but_orderable(self, duckdb_cursor, pandas):
        con = duckdb.connect()
        df = pandas.DataFrame([{"a": 1}, {"a": 3}, {"a": 2}])

        star_columns = list(con.execute('select * from df limit 0').fetchnumpy().keys())
        assert star_columns == ['a']

        descending = con.execute('select * from df order by rowid desc').fetchall()
        assert descending == [(2,), (3,), (1,)]

        with_rowid = con.execute('select rowid, * from df order by rowid').fetchall()
        assert with_rowid == [(0, 1), (1, 3), (2, 2)]

    @pytest.mark.parametrize('pandas', [NumpyPandas(), ArrowPandas()])
    def test_rowid_order_by_across_cte(self, duckdb_cursor, pandas):
        con = duckdb.connect()
        df = pandas.DataFrame([{"a": 11}, {"a": 22}, {"a": 33}])

        result = con.execute(
            """
            with t as (
                select rowid, * from df
            )
            select * from t order by rowid desc
            """
        ).fetchall()
        assert result == [(2, 33), (1, 22), (0, 11)]

    @pytest.mark.parametrize('pandas', [NumpyPandas(), ArrowPandas()])
    def test_module_sql_rowid(self, duckdb_cursor, pandas):
        df = pandas.DataFrame([{"a": 1}, {"a": 3}, {"a": 2}])

        result = duckdb.sql('select rowid, * from df order by rowid').fetchall()
        assert result == [(0, 1), (1, 3), (2, 2)]

    @pytest.mark.parametrize('pandas', [NumpyPandas(), ArrowPandas()])
    def test_module_sql_rowid_hidden_but_orderable(self, duckdb_cursor, pandas):
        df = pandas.DataFrame([{"a": 1}, {"a": 3}, {"a": 2}])

        star_columns = list(duckdb.sql('select * from df limit 0').fetchnumpy().keys())
        assert star_columns == ['a']

        descending = duckdb.sql('select * from df order by rowid desc').fetchall()
        assert descending == [(2,), (3,), (1,)]

        with_rowid = duckdb.sql('select rowid, * from df order by rowid').fetchall()
        assert with_rowid == [(0, 1), (1, 3), (2, 2)]

    @pytest.mark.parametrize('pandas', [NumpyPandas(), ArrowPandas()])
    def test_module_sql_relation_chaining_rowid_negative(self, duckdb_cursor, pandas):
        df = pandas.DataFrame([{"a": 1}, {"a": 3}, {"a": 2}])

        with pytest.raises(duckdb.BinderException, match='Referenced column "rowid" not found in FROM clause!'):
            duckdb.sql('from df').project('rowid, *').fetchall()

    def test_rowid_hidden_but_orderable_pyarrow_backed_dataframe(self, duckdb_cursor):
        pandas = ArrowPandas()
        if pandas.backend != 'pyarrow':
            pytest.skip("pyarrow-backed pandas DataFrame not available in this environment")

        con = duckdb.connect()
        df = pandas.DataFrame([{"a": 5}, {"a": 7}, {"a": 6}])

        star_columns = list(con.execute('select * from df limit 0').fetchnumpy().keys())
        assert star_columns == ['a']

        descending = con.execute('select * from df order by rowid desc').fetchall()
        assert descending == [(6,), (7,), (5,)]

        with_rowid = con.execute('select rowid, * from df order by rowid').fetchall()
        assert with_rowid == [(0, 5), (1, 7), (2, 6)]
