from functools import reduce
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    List,
    Dict,
    Optional,
    Tuple,
    Union,
    cast,
    overload,
)
import uuid
from keyword import iskeyword

import duckdb
from duckdb import ColumnExpression, Expression, StarExpression

from ._typing import ColumnOrName
from ..errors import PySparkTypeError, PySparkValueError, PySparkIndexError
from ..exception import ContributionsAcceptedError
from .column import Column
from .readwriter import DataFrameWriter
from .type_utils import duckdb_to_spark_schema
from .types import Row, StructType

if TYPE_CHECKING:
    import pyarrow as pa
    from pandas.core.frame import DataFrame as PandasDataFrame

    from .group import GroupedData, Grouping
    from .session import SparkSession

from ..errors import PySparkValueError
from .functions import _to_column_expr, col, lit


class DataFrame:
    def __init__(self, relation: duckdb.DuckDBPyRelation, session: "SparkSession"):
        self.relation = relation
        self.session = session
        self._schema = None
        if self.relation is not None:
            self._schema = duckdb_to_spark_schema(self.relation.columns, self.relation.types)

    def show(self, **kwargs) -> None:
        self.relation.show()

    def toPandas(self) -> "PandasDataFrame":
        return self.relation.df()

    def toArrow(self) -> "pa.Table":
        """
        Returns the contents of this :class:`DataFrame` as PyArrow ``pyarrow.Table``.

        This is only available if PyArrow is installed and available.

        .. versionadded:: 4.0.0

        Notes
        -----
        This method should only be used if the resulting PyArrow ``pyarrow.Table`` is
        expected to be small, as all the data is loaded into the driver's memory.

        This API is a developer API.

        Examples
        --------
        >>> df.toArrow()  # doctest: +SKIP
        pyarrow.Table
        age: int64
        name: string
        ----
        age: [[2,5]]
        name: [["Alice","Bob"]]
        """
        return self.relation.arrow()

    def createOrReplaceTempView(self, name: str) -> None:
        """Creates or replaces a local temporary view with this :class:`DataFrame`.

        The lifetime of this temporary table is tied to the :class:`SparkSession`
        that was used to create this :class:`DataFrame`.

        Parameters
        ----------
        name : str
            Name of the view.

        Examples
        --------
        Create a local temporary view named 'people'.

        >>> df = spark.createDataFrame([(2, "Alice"), (5, "Bob")], schema=["age", "name"])
        >>> df.createOrReplaceTempView("people")

        Replace the local temporary view.

        >>> df2 = df.filter(df.age > 3)
        >>> df2.createOrReplaceTempView("people")
        >>> df3 = spark.sql("SELECT * FROM people")
        >>> sorted(df3.collect()) == sorted(df2.collect())
        True
        >>> spark.catalog.dropTempView("people")
        True

        """
        self.relation.create_view(name, True)

    def createGlobalTempView(self, name: str) -> None:
        raise NotImplementedError

    def withColumnRenamed(self, columnName: str, newName: str) -> "DataFrame":
        if columnName not in self.relation:
            raise ValueError(f"DataFrame does not contain a column named {columnName}")
        cols = []
        for x in self.relation.columns:
            col = ColumnExpression(x)
            if x.casefold() == columnName.casefold():
                col = col.alias(newName)
            cols.append(col)
        rel = self.relation.select(*cols)
        return DataFrame(rel, self.session)

    def withColumn(self, columnName: str, col: Column) -> "DataFrame":
        if not isinstance(col, Column):
            raise PySparkTypeError(
                error_class="NOT_COLUMN",
                message_parameters={"arg_name": "col", "arg_type": type(col).__name__},
            )
        if columnName in self.relation:
            # We want to replace the existing column with this new expression
            cols = []
            for x in self.relation.columns:
                if x.casefold() == columnName.casefold():
                    cols.append(col.expr.alias(columnName))
                else:
                    cols.append(ColumnExpression(x))
        else:
            cols = [ColumnExpression(x) for x in self.relation.columns]
            cols.append(col.expr.alias(columnName))
        rel = self.relation.select(*cols)
        return DataFrame(rel, self.session)

    def withColumns(self, *colsMap: Dict[str, Column]) -> "DataFrame":
        """
        Returns a new :class:`DataFrame` by adding multiple columns or replacing the
        existing columns that have the same names.

        The colsMap is a map of column name and column, the column must only refer to attributes
        supplied by this Dataset. It is an error to add columns that refer to some other Dataset.

        .. versionadded:: 3.3.0
           Added support for multiple columns adding

        .. versionchanged:: 3.4.0
            Supports Spark Connect.

        Parameters
        ----------
        colsMap : dict
            a dict of column name and :class:`Column`. Currently, only a single map is supported.

        Returns
        -------
        :class:`DataFrame`
            DataFrame with new or replaced columns.

        Examples
        --------
        >>> df = spark.createDataFrame([(2, "Alice"), (5, "Bob")], schema=["age", "name"])
        >>> df.withColumns({'age2': df.age + 2, 'age3': df.age + 3}).show()
        +---+-----+----+----+
        |age| name|age2|age3|
        +---+-----+----+----+
        |  2|Alice|   4|   5|
        |  5|  Bob|   7|   8|
        +---+-----+----+----+
        """
        # Below code is to help enable kwargs in future.
        assert len(colsMap) == 1
        colsMap = colsMap[0]  # type: ignore[assignment]

        if not isinstance(colsMap, dict):
            raise PySparkTypeError(
                error_class="NOT_DICT",
                message_parameters={
                    "arg_name": "colsMap",
                    "arg_type": type(colsMap).__name__,
                },
            )

        column_names = list(colsMap.keys())
        columns = list(colsMap.values())

        # Compute this only once
        column_names_for_comparison = [x.casefold() for x in column_names]

        cols = []
        for x in self.relation.columns:
            if x.casefold() in column_names_for_comparison:
                idx = column_names_for_comparison.index(x)
                # We extract the column name from the originally passed
                # in ones, as the casing might be different than the one
                # in the relation
                col_name = column_names.pop(idx)
                col = columns.pop(idx)
                cols.append(col.expr.alias(col_name))
            else:
                cols.append(ColumnExpression(x))

        # In case anything is remaining, these are new columns
        # that we need to add to the DataFrame
        for col_name, col in zip(column_names, columns):
            cols.append(col.expr.alias(col_name))

        rel = self.relation.select(*cols)
        return DataFrame(rel, self.session)

    def withColumnsRenamed(self, colsMap: Dict[str, str]) -> "DataFrame":
        """
        Returns a new :class:`DataFrame` by renaming multiple columns.
        This is a no-op if the schema doesn't contain the given column names.

        .. versionadded:: 3.4.0
           Added support for multiple columns renaming

        Parameters
        ----------
        colsMap : dict
            a dict of existing column names and corresponding desired column names.
            Currently, only a single map is supported.

        Returns
        -------
        :class:`DataFrame`
            DataFrame with renamed columns.

        See Also
        --------
        :meth:`withColumnRenamed`

        Notes
        -----
        Support Spark Connect

        Examples
        --------
        >>> df = spark.createDataFrame([(2, "Alice"), (5, "Bob")], schema=["age", "name"])
        >>> df = df.withColumns({'age2': df.age + 2, 'age3': df.age + 3})
        >>> df.withColumnsRenamed({'age2': 'age4', 'age3': 'age5'}).show()
        +---+-----+----+----+
        |age| name|age4|age5|
        +---+-----+----+----+
        |  2|Alice|   4|   5|
        |  5|  Bob|   7|   8|
        +---+-----+----+----+
        """
        if not isinstance(colsMap, dict):
            raise PySparkTypeError(
                error_class="NOT_DICT",
                message_parameters={"arg_name": "colsMap", "arg_type": type(colsMap).__name__},
            )

        unknown_columns = set(colsMap.keys()) - set(self.relation.columns)
        if unknown_columns:
            raise ValueError(
                f"DataFrame does not contain column(s): {', '.join(unknown_columns)}"
            )

        # Compute this only once
        old_column_names = list(colsMap.keys())
        old_column_names_for_comparison = [x.casefold() for x in old_column_names]

        cols = []
        for x in self.relation.columns:
            col = ColumnExpression(x)
            if x.casefold() in old_column_names_for_comparison:
                idx = old_column_names.index(x)
                # We extract the column name from the originally passed
                # in ones, as the casing might be different than the one
                # in the relation
                col_name = old_column_names.pop(idx)
                new_col_name = colsMap[col_name]
                col = col.alias(new_col_name)
            cols.append(col)

        rel = self.relation.select(*cols)
        return DataFrame(rel, self.session)



    def transform(
        self, func: Callable[..., "DataFrame"], *args: Any, **kwargs: Any
    ) -> "DataFrame":
        """Returns a new :class:`DataFrame`. Concise syntax for chaining custom transformations.

        .. versionadded:: 3.0.0

        .. versionchanged:: 3.4.0
            Supports Spark Connect.

        Parameters
        ----------
        func : function
            a function that takes and returns a :class:`DataFrame`.
        *args
            Positional arguments to pass to func.

            .. versionadded:: 3.3.0
        **kwargs
            Keyword arguments to pass to func.

            .. versionadded:: 3.3.0

        Returns
        -------
        :class:`DataFrame`
            Transformed DataFrame.

        Examples
        --------
        >>> from pyspark.sql.functions import col
        >>> df = spark.createDataFrame([(1, 1.0), (2, 2.0)], ["int", "float"])
        >>> def cast_all_to_int(input_df):
        ...     return input_df.select([col(col_name).cast("int") for col_name in input_df.columns])
        ...
        >>> def sort_columns_asc(input_df):
        ...     return input_df.select(*sorted(input_df.columns))
        ...
        >>> df.transform(cast_all_to_int).transform(sort_columns_asc).show()
        +-----+---+
        |float|int|
        +-----+---+
        |    1|  1|
        |    2|  2|
        +-----+---+

        >>> def add_n(input_df, n):
        ...     return input_df.select([(col(col_name) + n).alias(col_name)
        ...                             for col_name in input_df.columns])
        >>> df.transform(add_n, 1).transform(add_n, n=10).show()
        +---+-----+
        |int|float|
        +---+-----+
        | 12| 12.0|
        | 13| 13.0|
        +---+-----+
        """
        result = func(self, *args, **kwargs)
        assert isinstance(result, DataFrame), (
            "Func returned an instance of type [%s], "
            "should have been DataFrame." % type(result)
        )
        return result

    def sort(
        self, *cols: Union[str, Column, List[Union[str, Column]]], **kwargs: Any
    ) -> "DataFrame":
        """Returns a new :class:`DataFrame` sorted by the specified column(s).

        Parameters
        ----------
        cols : str, list, or :class:`Column`, optional
             list of :class:`Column` or column names to sort by.

        Other Parameters
        ----------------
        ascending : bool or list, optional, default True
            boolean or list of boolean.
            Sort ascending vs. descending. Specify list for multiple sort orders.
            If a list is specified, the length of the list must equal the length of the `cols`.

        Returns
        -------
        :class:`DataFrame`
            Sorted DataFrame.

        Examples
        --------
        >>> from pyspark.sql.functions import desc, asc
        >>> df = spark.createDataFrame([
        ...     (2, "Alice"), (5, "Bob")], schema=["age", "name"])

        Sort the DataFrame in ascending order.

        >>> df.sort(asc("age")).show()
        +---+-----+
        |age| name|
        +---+-----+
        |  2|Alice|
        |  5|  Bob|
        +---+-----+

        Sort the DataFrame in descending order.

        >>> df.sort(df.age.desc()).show()
        +---+-----+
        |age| name|
        +---+-----+
        |  5|  Bob|
        |  2|Alice|
        +---+-----+
        >>> df.orderBy(df.age.desc()).show()
        +---+-----+
        |age| name|
        +---+-----+
        |  5|  Bob|
        |  2|Alice|
        +---+-----+
        >>> df.sort("age", ascending=False).show()
        +---+-----+
        |age| name|
        +---+-----+
        |  5|  Bob|
        |  2|Alice|
        +---+-----+

        Specify multiple columns

        >>> df = spark.createDataFrame([
        ...     (2, "Alice"), (2, "Bob"), (5, "Bob")], schema=["age", "name"])
        >>> df.orderBy(desc("age"), "name").show()
        +---+-----+
        |age| name|
        +---+-----+
        |  5|  Bob|
        |  2|Alice|
        |  2|  Bob|
        +---+-----+

        Specify multiple columns for sorting order at `ascending`.

        >>> df.orderBy(["age", "name"], ascending=[False, False]).show()
        +---+-----+
        |age| name|
        +---+-----+
        |  5|  Bob|
        |  2|  Bob|
        |  2|Alice|
        +---+-----+
        """
        if not cols:
            raise PySparkValueError(
                error_class="CANNOT_BE_EMPTY",
                message_parameters={"item": "column"},
            )
        if len(cols) == 1 and isinstance(cols[0], list):
            cols = cols[0]

        columns = []
        for c in cols:
            _c = c
            if isinstance(c, str):
                _c = col(c)
            elif isinstance(c, int) and not isinstance(c, bool):
                # ordinal is 1-based
                if c > 0:
                    _c = self[c - 1]
                # negative ordinal means sort by desc
                elif c < 0:
                    _c = self[-c - 1].desc()
                else:
                    raise PySparkIndexError(
                        error_class="ZERO_INDEX",
                        message_parameters={},
                    )
            columns.append(_c)

        ascending = kwargs.get("ascending", True)

        if isinstance(ascending, (bool, int)):
            if not ascending:
                columns = [c.desc() for c in columns]
        elif isinstance(ascending, list):
            columns = [c if asc else c.desc() for asc, c in zip(ascending, columns)]
        else:
            raise PySparkTypeError(
                error_class="NOT_BOOL_OR_LIST",
                message_parameters={"arg_name": "ascending", "arg_type": type(ascending).__name__},
            )

        columns = [_to_column_expr(c) for c in columns]
        rel = self.relation.sort(*columns)
        return DataFrame(rel, self.session)

    orderBy = sort

    def head(self, n: Optional[int] = None) -> Union[Optional[Row], List[Row]]:
        if n is None:
            rs = self.head(1)
            return rs[0] if rs else None
        return self.take(n)

    first = head

    def take(self, num: int) -> List[Row]:
        return self.limit(num).collect()

    def filter(self, condition: "ColumnOrName") -> "DataFrame":
        """Filters rows using the given condition.

        :func:`where` is an alias for :func:`filter`.

        Parameters
        ----------
        condition : :class:`Column` or str
            a :class:`Column` of :class:`types.BooleanType`
            or a string of SQL expressions.

        Returns
        -------
        :class:`DataFrame`
            Filtered DataFrame.

        Examples
        --------
        >>> df = spark.createDataFrame([
        ...     (2, "Alice"), (5, "Bob")], schema=["age", "name"])

        Filter by :class:`Column` instances.

        >>> df.filter(df.age > 3).show()
        +---+----+
        |age|name|
        +---+----+
        |  5| Bob|
        +---+----+
        >>> df.where(df.age == 2).show()
        +---+-----+
        |age| name|
        +---+-----+
        |  2|Alice|
        +---+-----+

        Filter by SQL expression in a string.

        >>> df.filter("age > 3").show()
        +---+----+
        |age|name|
        +---+----+
        |  5| Bob|
        +---+----+
        >>> df.where("age = 2").show()
        +---+-----+
        |age| name|
        +---+-----+
        |  2|Alice|
        +---+-----+
        """
        if isinstance(condition, Column):
            cond = condition.expr
        elif isinstance(condition, str):
            cond = condition
        else:
            raise PySparkTypeError(
                error_class="NOT_COLUMN_OR_STR",
                message_parameters={"arg_name": "condition", "arg_type": type(condition).__name__},
            )
        rel = self.relation.filter(cond)
        return DataFrame(rel, self.session)

    where = filter

    def select(self, *cols) -> "DataFrame":
        cols = list(cols)
        if len(cols) == 1:
            cols = cols[0]
        if isinstance(cols, list):
            projections = [
                x.expr if isinstance(x, Column) else ColumnExpression(x) for x in cols
            ]
        else:
            projections = [
                cols.expr if isinstance(cols, Column) else ColumnExpression(cols)
            ]
        rel = self.relation.select(*projections)
        return DataFrame(rel, self.session)

    @property
    def columns(self) -> List[str]:
        """Returns all column names as a list.

        Examples
        --------
        >>> df.columns
        ['age', 'name']
        """
        return [f.name for f in self.schema.fields]

    def _ipython_key_completions_(self) -> List[str]:
        # Provides tab-completion for column names in PySpark DataFrame
        # when accessed in bracket notation, e.g. df['<TAB>]
        return self.columns

    def __dir__(self) -> List[str]:
        out = set(super().__dir__())
        out.update(c for c in self.columns if c.isidentifier() and not iskeyword(c))
        return sorted(out)

    def join(
        self,
        other: "DataFrame",
        on: Optional[Union[str, List[str], Column, List[Column]]] = None,
        how: Optional[str] = None,
    ) -> "DataFrame":
        """Joins with another :class:`DataFrame`, using the given join expression.

        Parameters
        ----------
        other : :class:`DataFrame`
            Right side of the join
        on : str, list or :class:`Column`, optional
            a string for the join column name, a list of column names,
            a join expression (Column), or a list of Columns.
            If `on` is a string or a list of strings indicating the name of the join column(s),
            the column(s) must exist on both sides, and this performs an equi-join.
        how : str, optional
            default ``inner``. Must be one of: ``inner``, ``cross``, ``outer``,
            ``full``, ``fullouter``, ``full_outer``, ``left``, ``leftouter``, ``left_outer``,
            ``right``, ``rightouter``, ``right_outer``, ``semi``, ``leftsemi``, ``left_semi``,
            ``anti``, ``leftanti`` and ``left_anti``.

        Returns
        -------
        :class:`DataFrame`
            Joined DataFrame.

        Examples
        --------
        The following performs a full outer join between ``df1`` and ``df2``.

        >>> from pyspark.sql import Row
        >>> from pyspark.sql.functions import desc
        >>> df = spark.createDataFrame([(2, "Alice"), (5, "Bob")]).toDF("age", "name")
        >>> df2 = spark.createDataFrame([Row(height=80, name="Tom"), Row(height=85, name="Bob")])
        >>> df3 = spark.createDataFrame([Row(age=2, name="Alice"), Row(age=5, name="Bob")])
        >>> df4 = spark.createDataFrame([
        ...     Row(age=10, height=80, name="Alice"),
        ...     Row(age=5, height=None, name="Bob"),
        ...     Row(age=None, height=None, name="Tom"),
        ...     Row(age=None, height=None, name=None),
        ... ])

        Inner join on columns (default)

        >>> df.join(df2, 'name').select(df.name, df2.height).show()
        +----+------+
        |name|height|
        +----+------+
        | Bob|    85|
        +----+------+
        >>> df.join(df4, ['name', 'age']).select(df.name, df.age).show()
        +----+---+
        |name|age|
        +----+---+
        | Bob|  5|
        +----+---+

        Outer join for both DataFrames on the 'name' column.

        >>> df.join(df2, df.name == df2.name, 'outer').select(
        ...     df.name, df2.height).sort(desc("name")).show()
        +-----+------+
        | name|height|
        +-----+------+
        |  Bob|    85|
        |Alice|  NULL|
        | NULL|    80|
        +-----+------+
        >>> df.join(df2, 'name', 'outer').select('name', 'height').sort(desc("name")).show()
        +-----+------+
        | name|height|
        +-----+------+
        |  Tom|    80|
        |  Bob|    85|
        |Alice|  NULL|
        +-----+------+

        Outer join for both DataFrams with multiple columns.

        >>> df.join(
        ...     df3,
        ...     [df.name == df3.name, df.age == df3.age],
        ...     'outer'
        ... ).select(df.name, df3.age).show()
        +-----+---+
        | name|age|
        +-----+---+
        |Alice|  2|
        |  Bob|  5|
        +-----+---+
        """

        if on is not None and not isinstance(on, list):
            on = [on]  # type: ignore[assignment]
        if on is not None and not all([isinstance(x, str) for x in on]):
            assert isinstance(on, list)
            # Get (or create) the Expressions from the list of Columns
            on = [_to_column_expr(x) for x in on]

            # & all the Expressions together to form one Expression
            assert isinstance(
                on[0], Expression
            ), "on should be Column or list of Column"
            on = reduce(lambda x, y: x.__and__(y), cast(List[Expression], on))


        if on is None and how is None:
            result = self.relation.join(other.relation)
        else:
            if how is None:
                how = "inner"
            if on is None:
                on = "true"
            elif isinstance(on, list) and all([isinstance(x, str) for x in on]):
                # Passed directly through as a list of strings
                on = on
            else:
                on = str(on)
            assert isinstance(how, str), "how should be a string"

            def map_to_recognized_jointype(how):
                known_aliases = {
                    "inner": [],
                    "outer": ["full", "fullouter", "full_outer"],
                    "left": ["leftouter", "left_outer"],
                    "right": ["rightouter", "right_outer"],
                    "anti": ["leftanti", "left_anti"],
                    "semi": ["leftsemi", "left_semi"],
                }
                mapped_type = None
                for type, aliases in known_aliases.items():
                    if how == type or how in aliases:
                        mapped_type = type
                        break

                if not mapped_type:
                    mapped_type = how
                return mapped_type

            how = map_to_recognized_jointype(how)
            result = self.relation.join(other.relation, on, how)
        return DataFrame(result, self.session)

    def crossJoin(self, other: "DataFrame") -> "DataFrame":
        """Returns the cartesian product with another :class:`DataFrame`.

        .. versionadded:: 2.1.0

        .. versionchanged:: 3.4.0
            Supports Spark Connect.

        Parameters
        ----------
        other : :class:`DataFrame`
            Right side of the cartesian product.

        Returns
        -------
        :class:`DataFrame`
            Joined DataFrame.

        Examples
        --------
        >>> from pyspark.sql import Row
        >>> df = spark.createDataFrame(
        ...     [(14, "Tom"), (23, "Alice"), (16, "Bob")], ["age", "name"])
        >>> df2 = spark.createDataFrame(
        ...     [Row(height=80, name="Tom"), Row(height=85, name="Bob")])
        >>> df.crossJoin(df2.select("height")).select("age", "name", "height").show()
        +---+-----+------+
        |age| name|height|
        +---+-----+------+
        | 14|  Tom|    80|
        | 14|  Tom|    85|
        | 23|Alice|    80|
        | 23|Alice|    85|
        | 16|  Bob|    80|
        | 16|  Bob|    85|
        +---+-----+------+
        """
        return DataFrame(self.relation.cross(other.relation), self.session)

    def alias(self, alias: str) -> "DataFrame":
        """Returns a new :class:`DataFrame` with an alias set.

        Parameters
        ----------
        alias : str
            an alias name to be set for the :class:`DataFrame`.

        Returns
        -------
        :class:`DataFrame`
            Aliased DataFrame.

        Examples
        --------
        >>> from pyspark.sql.functions import col, desc
        >>> df = spark.createDataFrame(
        ...     [(14, "Tom"), (23, "Alice"), (16, "Bob")], ["age", "name"])
        >>> df_as1 = df.alias("df_as1")
        >>> df_as2 = df.alias("df_as2")
        >>> joined_df = df_as1.join(df_as2, col("df_as1.name") == col("df_as2.name"), 'inner')
        >>> joined_df.select(
        ...     "df_as1.name", "df_as2.name", "df_as2.age").sort(desc("df_as1.name")).show()
        +-----+-----+---+
        | name| name|age|
        +-----+-----+---+
        |  Tom|  Tom| 14|
        |  Bob|  Bob| 16|
        |Alice|Alice| 23|
        +-----+-----+---+
        """
        assert isinstance(alias, str), "alias should be a string"
        return DataFrame(self.relation.set_alias(alias), self.session)

    def drop(self, *cols: "ColumnOrName") -> "DataFrame":  # type: ignore[misc]
        exclude = []
        for col in cols:
            if isinstance(col, str):
                exclude.append(col)
            elif isinstance(col, Column):
                exclude.append(col.expr.get_name())
            else:
                raise PySparkTypeError(
                    error_class="NOT_COLUMN_OR_STR",
                    message_parameters={"arg_name": "col", "arg_type": type(col).__name__},
                )
        # Filter out the columns that don't exist in the relation
        exclude = [x for x in exclude if x in self.relation.columns]
        expr = StarExpression(exclude=exclude)
        return DataFrame(self.relation.select(expr), self.session)

    def __repr__(self) -> str:
        return str(self.relation)

    def limit(self, num: int) -> "DataFrame":
        """Limits the result count to the number specified.

        Parameters
        ----------
        num : int
            Number of records to return. Will return this number of records
            or all records if the DataFrame contains less than this number of records.

        Returns
        -------
        :class:`DataFrame`
            Subset of the records

        Examples
        --------
        >>> df = spark.createDataFrame(
        ...     [(14, "Tom"), (23, "Alice"), (16, "Bob")], ["age", "name"])
        >>> df.limit(1).show()
        +---+----+
        |age|name|
        +---+----+
        | 14| Tom|
        +---+----+
        >>> df.limit(0).show()
        +---+----+
        |age|name|
        +---+----+
        +---+----+
        """
        rel = self.relation.limit(num)
        return DataFrame(rel, self.session)

    def __contains__(self, item: str):
        """
        Check if the :class:`DataFrame` contains a column by the name of `item`
        """
        return item in self.relation

    @property
    def schema(self) -> StructType:
        """Returns the schema of this :class:`DataFrame` as a :class:`duckdb.experimental.spark.sql.types.StructType`.

        Examples
        --------
        >>> df.schema
        StructType([StructField('age', IntegerType(), True),
                    StructField('name', StringType(), True)])
        """
        return self._schema

    @overload
    def __getitem__(self, item: Union[int, str]) -> Column:
        ...

    @overload
    def __getitem__(self, item: Union[Column, List, Tuple]) -> "DataFrame":
        ...

    def __getitem__(
        self, item: Union[int, str, Column, List, Tuple]
    ) -> Union[Column, "DataFrame"]:
        """Returns the column as a :class:`Column`.

        Examples
        --------
        >>> df.select(df['age']).collect()
        [Row(age=2), Row(age=5)]
        >>> df[ ["name", "age"]].collect()
        [Row(name='Alice', age=2), Row(name='Bob', age=5)]
        >>> df[ df.age > 3 ].collect()
        [Row(age=5, name='Bob')]
        >>> df[df[0] > 3].collect()
        [Row(age=5, name='Bob')]
        """
        if isinstance(item, str):
            return Column(duckdb.ColumnExpression(self.relation.alias, item))
        elif isinstance(item, Column):
            return self.filter(item)
        elif isinstance(item, (list, tuple)):
            return self.select(*item)
        elif isinstance(item, int):
            return col(self._schema[item].name)
        else:
            raise TypeError(f"Unexpected item type: {type(item)}")

    def __getattr__(self, name: str) -> Column:
        """Returns the :class:`Column` denoted by ``name``.

        Examples
        --------
        >>> df.select(df.age).collect()
        [Row(age=2), Row(age=5)]
        """
        if name not in self.relation.columns:
            raise AttributeError(
                "'%s' object has no attribute '%s'" % (self.__class__.__name__, name)
            )
        return Column(duckdb.ColumnExpression(self.relation.alias, name))

    @overload
    def groupBy(self, *cols: "ColumnOrName") -> "GroupedData":
        ...

    @overload
    def groupBy(self, __cols: Union[List[Column], List[str]]) -> "GroupedData":
        ...

    def groupBy(self, *cols: "ColumnOrName") -> "GroupedData":  # type: ignore[misc]
        """Groups the :class:`DataFrame` using the specified columns,
        so we can run aggregation on them. See :class:`GroupedData`
        for all the available aggregate functions.

        :func:`groupby` is an alias for :func:`groupBy`.

        Parameters
        ----------
        cols : list, str or :class:`Column`
            columns to group by.
            Each element should be a column name (string) or an expression (:class:`Column`)
            or list of them.

        Returns
        -------
        :class:`GroupedData`
            Grouped data by given columns.

        Examples
        --------
        >>> df = spark.createDataFrame([
        ...     (2, "Alice"), (2, "Bob"), (2, "Bob"), (5, "Bob")], schema=["age", "name"])

        Empty grouping columns triggers a global aggregation.

        >>> df.groupBy().avg().show()
        +--------+
        |avg(age)|
        +--------+
        |    2.75|
        +--------+

        Group-by 'name', and specify a dictionary to calculate the summation of 'age'.

        >>> df.groupBy("name").agg({"age": "sum"}).sort("name").show()
        +-----+--------+
        | name|sum(age)|
        +-----+--------+
        |Alice|       2|
        |  Bob|       9|
        +-----+--------+

        Group-by 'name', and calculate maximum values.

        >>> df.groupBy(df.name).max().sort("name").show()
        +-----+--------+
        | name|max(age)|
        +-----+--------+
        |Alice|       2|
        |  Bob|       5|
        +-----+--------+

        Group-by 'name' and 'age', and calculate the number of rows in each group.

        >>> df.groupBy(["name", df.age]).count().sort("name", "age").show()
        +-----+---+-----+
        | name|age|count|
        +-----+---+-----+
        |Alice|  2|    1|
        |  Bob|  2|    2|
        |  Bob|  5|    1|
        +-----+---+-----+
        """
        from .group import GroupedData, Grouping

        if len(cols) == 1 and isinstance(cols[0], list):
            columns = cols[0]
        else:
            columns = cols
        return GroupedData(Grouping(*columns), self)

    groupby = groupBy

    @property
    def write(self) -> DataFrameWriter:
        return DataFrameWriter(self)

    def printSchema(self):
        raise ContributionsAcceptedError

    def union(self, other: "DataFrame") -> "DataFrame":
        """Return a new :class:`DataFrame` containing union of rows in this and another
        :class:`DataFrame`.

        Parameters
        ----------
        other : :class:`DataFrame`
            Another :class:`DataFrame` that needs to be unioned

        Returns
        -------
        :class:`DataFrame`

        See Also
        --------
        DataFrame.unionAll

        Notes
        -----
        This is equivalent to `UNION ALL` in SQL. To do a SQL-style set union
        (that does deduplication of elements), use this function followed by :func:`distinct`.

        Also as standard in SQL, this function resolves columns by position (not by name).

        Examples
        --------
        >>> df1 = spark.createDataFrame([[1, 2, 3]], ["col0", "col1", "col2"])
        >>> df2 = spark.createDataFrame([[4, 5, 6]], ["col1", "col2", "col0"])
        >>> df1.union(df2).show()
        +----+----+----+
        |col0|col1|col2|
        +----+----+----+
        |   1|   2|   3|
        |   4|   5|   6|
        +----+----+----+
        >>> df1.union(df1).show()
        +----+----+----+
        |col0|col1|col2|
        +----+----+----+
        |   1|   2|   3|
        |   1|   2|   3|
        +----+----+----+
        """
        return DataFrame(self.relation.union(other.relation), self.session)

    unionAll = union

    def unionByName(
        self, other: "DataFrame", allowMissingColumns: bool = False
    ) -> "DataFrame":
        """Returns a new :class:`DataFrame` containing union of rows in this and another
        :class:`DataFrame`.

        This is different from both `UNION ALL` and `UNION DISTINCT` in SQL. To do a SQL-style set
        union (that does deduplication of elements), use this function followed by :func:`distinct`.

        .. versionadded:: 2.3.0

        .. versionchanged:: 3.4.0
            Supports Spark Connect.

        Parameters
        ----------
        other : :class:`DataFrame`
            Another :class:`DataFrame` that needs to be combined.
        allowMissingColumns : bool, optional, default False
           Specify whether to allow missing columns.

           .. versionadded:: 3.1.0

        Returns
        -------
        :class:`DataFrame`
            Combined DataFrame.

        Examples
        --------
        The difference between this function and :func:`union` is that this function
        resolves columns by name (not by position):

        >>> df1 = spark.createDataFrame([[1, 2, 3]], ["col0", "col1", "col2"])
        >>> df2 = spark.createDataFrame([[4, 5, 6]], ["col1", "col2", "col0"])
        >>> df1.unionByName(df2).show()
        +----+----+----+
        |col0|col1|col2|
        +----+----+----+
        |   1|   2|   3|
        |   6|   4|   5|
        +----+----+----+

        When the parameter `allowMissingColumns` is ``True``, the set of column names
        in this and other :class:`DataFrame` can differ; missing columns will be filled with null.
        Further, the missing columns of this :class:`DataFrame` will be added at the end
        in the schema of the union result:

        >>> df1 = spark.createDataFrame([[1, 2, 3]], ["col0", "col1", "col2"])
        >>> df2 = spark.createDataFrame([[4, 5, 6]], ["col1", "col2", "col3"])
        >>> df1.unionByName(df2, allowMissingColumns=True).show()
        +----+----+----+----+
        |col0|col1|col2|col3|
        +----+----+----+----+
        |   1|   2|   3|NULL|
        |NULL|   4|   5|   6|
        +----+----+----+----+
        """
        if allowMissingColumns:
            cols = []
            for col in self.relation.columns:
                if col in other.relation.columns:
                    cols.append(col)
                else:
                    cols.append(lit(None))
            other = other.select(*cols)
        else:
            other = other.select(*self.relation.columns)

        return DataFrame(self.relation.union(other.relation), self.session)

    def intersect(self, other: "DataFrame") -> "DataFrame":
        """Return a new :class:`DataFrame` containing rows only in
        both this :class:`DataFrame` and another :class:`DataFrame`.
        Note that any duplicates are removed. To preserve duplicates
        use :func:`intersectAll`.

        .. versionadded:: 1.3.0

        .. versionchanged:: 3.4.0
            Supports Spark Connect.

        Parameters
        ----------
        other : :class:`DataFrame`
            Another :class:`DataFrame` that needs to be combined.

        Returns
        -------
        :class:`DataFrame`
            Combined DataFrame.

        Notes
        -----
        This is equivalent to `INTERSECT` in SQL.

        Examples
        --------
        >>> df1 = spark.createDataFrame([("a", 1), ("a", 1), ("b", 3), ("c", 4)], ["C1", "C2"])
        >>> df2 = spark.createDataFrame([("a", 1), ("a", 1), ("b", 3)], ["C1", "C2"])
        >>> df1.intersect(df2).sort(df1.C1.desc()).show()
        +---+---+
        | C1| C2|
        +---+---+
        |  b|  3|
        |  a|  1|
        +---+---+
        """
        return self.intersectAll(other).drop_duplicates()

    def intersectAll(self, other: "DataFrame") -> "DataFrame":
        """Return a new :class:`DataFrame` containing rows in both this :class:`DataFrame`
        and another :class:`DataFrame` while preserving duplicates.

        This is equivalent to `INTERSECT ALL` in SQL. As standard in SQL, this function
        resolves columns by position (not by name).

        .. versionadded:: 2.4.0

        .. versionchanged:: 3.4.0
            Supports Spark Connect.

        Parameters
        ----------
        other : :class:`DataFrame`
            Another :class:`DataFrame` that needs to be combined.

        Returns
        -------
        :class:`DataFrame`
            Combined DataFrame.

        Examples
        --------
        >>> df1 = spark.createDataFrame([("a", 1), ("a", 1), ("b", 3), ("c", 4)], ["C1", "C2"])
        >>> df2 = spark.createDataFrame([("a", 1), ("a", 1), ("b", 3)], ["C1", "C2"])
        >>> df1.intersectAll(df2).sort("C1", "C2").show()
        +---+---+
        | C1| C2|
        +---+---+
        |  a|  1|
        |  a|  1|
        |  b|  3|
        +---+---+
        """
        return DataFrame(self.relation.intersect(other.relation), self.session)

    def exceptAll(self, other: "DataFrame") -> "DataFrame":
        """Return a new :class:`DataFrame` containing rows in this :class:`DataFrame` but
        not in another :class:`DataFrame` while preserving duplicates.

        This is equivalent to `EXCEPT ALL` in SQL.
        As standard in SQL, this function resolves columns by position (not by name).

        .. versionadded:: 2.4.0

        .. versionchanged:: 3.4.0
            Supports Spark Connect.

        Parameters
        ----------
        other : :class:`DataFrame`
            The other :class:`DataFrame` to compare to.

        Returns
        -------
        :class:`DataFrame`

        Examples
        --------
        >>> df1 = spark.createDataFrame(
        ...         [("a", 1), ("a", 1), ("a", 1), ("a", 2), ("b",  3), ("c", 4)], ["C1", "C2"])
        >>> df2 = spark.createDataFrame([("a", 1), ("b", 3)], ["C1", "C2"])
        >>> df1.exceptAll(df2).show()
        +---+---+
        | C1| C2|
        +---+---+
        |  a|  1|
        |  a|  1|
        |  a|  2|
        |  c|  4|
        +---+---+

        """
        return DataFrame(self.relation.except_(other.relation), self.session)

    def dropDuplicates(self, subset: Optional[List[str]] = None) -> "DataFrame":
        """Return a new :class:`DataFrame` with duplicate rows removed,
        optionally only considering certain columns.

        For a static batch :class:`DataFrame`, it just drops duplicate rows. For a streaming
        :class:`DataFrame`, it will keep all data across triggers as intermediate state to drop
        duplicates rows. You can use :func:`withWatermark` to limit how late the duplicate data can
        be and the system will accordingly limit the state. In addition, data older than
        watermark will be dropped to avoid any possibility of duplicates.

        :func:`drop_duplicates` is an alias for :func:`dropDuplicates`.

        Parameters
        ----------
        subset : List of column names, optional
            List of columns to use for duplicate comparison (default All columns).

        Returns
        -------
        :class:`DataFrame`
            DataFrame without duplicates.

        Examples
        --------
        >>> from pyspark.sql import Row
        >>> df = spark.createDataFrame([
        ...     Row(name='Alice', age=5, height=80),
        ...     Row(name='Alice', age=5, height=80),
        ...     Row(name='Alice', age=10, height=80)
        ... ])

        Deduplicate the same rows.

        >>> df.dropDuplicates().show()
        +-----+---+------+
        | name|age|height|
        +-----+---+------+
        |Alice|  5|    80|
        |Alice| 10|    80|
        +-----+---+------+

        Deduplicate values on 'name' and 'height' columns.

        >>> df.dropDuplicates(['name', 'height']).show()
        +-----+---+------+
        | name|age|height|
        +-----+---+------+
        |Alice|  5|    80|
        +-----+---+------+
        """
        if subset:
            rn_col = f"tmp_col_{uuid.uuid1().hex}"
            subset_str = ', '.join([f'"{c}"' for c in subset])
            window_spec = f"OVER(PARTITION BY {subset_str}) AS {rn_col}"
            df = DataFrame(self.relation.row_number(window_spec, "*"), self.session)
            return df.filter(f"{rn_col} = 1").drop(rn_col)

        return self.distinct()

    drop_duplicates = dropDuplicates


    def distinct(self) -> "DataFrame":
        """Returns a new :class:`DataFrame` containing the distinct rows in this :class:`DataFrame`.

        Returns
        -------
        :class:`DataFrame`
            DataFrame with distinct records.

        Examples
        --------
        >>> df = spark.createDataFrame(
        ...     [(14, "Tom"), (23, "Alice"), (23, "Alice")], ["age", "name"])

        Return the number of distinct rows in the :class:`DataFrame`

        >>> df.distinct().count()
        2
        """
        distinct_rel = self.relation.distinct()
        return DataFrame(distinct_rel, self.session)

    def count(self) -> int:
        """Returns the number of rows in this :class:`DataFrame`.

        Returns
        -------
        int
            Number of rows.

        Examples
        --------
        >>> df = spark.createDataFrame(
        ...     [(14, "Tom"), (23, "Alice"), (16, "Bob")], ["age", "name"])

        Return the number of rows in the :class:`DataFrame`.

        >>> df.count()
        3
        """
        count_rel = self.relation.count("*")
        return int(count_rel.fetchone()[0])

    def _cast_types(self, *types) -> "DataFrame":
        existing_columns = self.relation.columns
        types_count = len(types)
        assert types_count == len(existing_columns)

        cast_expressions = [
            f"{existing}::{target_type} as {existing}"
            for existing, target_type in zip(existing_columns, types)
        ]
        cast_expressions = ", ".join(cast_expressions)
        new_rel = self.relation.project(cast_expressions)
        return DataFrame(new_rel, self.session)

    def toDF(self, *cols) -> "DataFrame":
        existing_columns = self.relation.columns
        column_count = len(cols)
        if column_count != len(existing_columns):
            raise PySparkValueError(
                message="Provided column names and number of columns in the DataFrame don't match"
            )

        existing_columns = [ColumnExpression(x) for x in existing_columns]
        projections = [
            existing.alias(new) for existing, new in zip(existing_columns, cols)
        ]
        new_rel = self.relation.project(*projections)
        return DataFrame(new_rel, self.session)

    def collect(self) -> List[Row]:
        columns = self.relation.columns
        result = self.relation.fetchall()

        def construct_row(values, names) -> Row:
            row = tuple.__new__(Row, list(values))
            row.__fields__ = list(names)
            return row

        rows = [construct_row(x, columns) for x in result]
        return rows


__all__ = ["DataFrame"]
