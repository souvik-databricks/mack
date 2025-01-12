from typing import List, Union
from itertools import combinations

from delta import DeltaTable
import pyspark
from pyspark.sql.functions import count, col, row_number, md5, concat_ws
from pyspark.sql.window import Window
from pyspark.sql.dataframe import DataFrame


def type_2_scd_upsert(path, updates_df, primary_key, attr_col_names):
    return type_2_scd_generic_upsert(
        path,
        updates_df,
        primary_key,
        attr_col_names,
        "is_current",
        "effective_time",
        "end_time",
    )


def type_2_scd_generic_upsert(
    path,
    updates_df,
    primary_key,
    attr_col_names,
    is_current_col_name,
    effective_time_col_name,
    end_time_col_name,
):
    base_table = DeltaTable.forPath(pyspark.sql.SparkSession.getActiveSession(), path)
    # validate the existing Delta table
    base_col_names = base_table.toDF().columns
    required_base_col_names = (
        [primary_key]
        + attr_col_names
        + [is_current_col_name, effective_time_col_name, end_time_col_name]
    )
    if sorted(base_col_names) != sorted(required_base_col_names):
        raise TypeError(
            f"The base table has these columns '{base_col_names}', but these columns are required '{required_base_col_names}'"
        )
    # validate the updates DataFrame
    updates_col_names = updates_df.columns
    required_updates_col_names = (
        [primary_key] + attr_col_names + [effective_time_col_name]
    )
    if sorted(updates_col_names) != sorted(required_updates_col_names):
        raise TypeError(
            f"The updates DataFrame has these columns '{updates_col_names}', but these columns are required '{required_updates_col_names}'"
        )

    # perform the upsert
    updates_attrs = list(
        map(lambda attr: f"updates.{attr} <> base.{attr}", attr_col_names)
    )
    updates_attrs = " OR ".join(updates_attrs)
    staged_updates_attrs = list(
        map(lambda attr: f"staged_updates.{attr} <> base.{attr}", attr_col_names)
    )
    staged_updates_attrs = " OR ".join(staged_updates_attrs)
    staged_part_1 = (
        updates_df.alias("updates")
        .join(base_table.toDF().alias("base"), primary_key)
        .where(f"base.{is_current_col_name} = true AND ({updates_attrs})")
        .selectExpr("NULL as mergeKey", "updates.*")
    )
    staged_part_2 = updates_df.selectExpr(f"{primary_key} as mergeKey", "*")
    staged_updates = staged_part_1.union(staged_part_2)
    thing = {}
    for attr in attr_col_names:
        thing[attr] = f"staged_updates.{attr}"
    thing2 = {
        primary_key: f"staged_updates.{primary_key}",
        is_current_col_name: "true",
        effective_time_col_name: f"staged_updates.{effective_time_col_name}",
        end_time_col_name: "null",
    }
    res_thing = {**thing, **thing2}
    res = (
        base_table.alias("base")
        .merge(
            source=staged_updates.alias("staged_updates"),
            condition=pyspark.sql.functions.expr(
                f"base.{primary_key} = mergeKey AND base.{is_current_col_name} = true AND ({staged_updates_attrs})"
            ),
        )
        .whenMatchedUpdate(
            set={
                is_current_col_name: "false",
                end_time_col_name: f"staged_updates.{effective_time_col_name}",
            }
        )
        .whenNotMatchedInsert(values=res_thing)
        .execute()
    )
    return res


def kill_duplicates(delta_table: DeltaTable, duplication_columns: List[str] = None):
    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    if not duplication_columns or len(duplication_columns) == 0:
        raise TypeError("Duplication columns must be specified")

    data_frame = delta_table.toDF()

    # Make sure that all the required columns are present in the provided delta table
    data_frame_columns = data_frame.columns
    for required_column in duplication_columns:
        if required_column not in data_frame_columns:
            raise TypeError(
                f"The base table has these columns '{data_frame_columns}', but these columns are required '{duplication_columns}'"
            )

    q = []

    duplicate_records = (
        data_frame.withColumn(
            "amount_of_records",
            count("*").over(Window.partitionBy(duplication_columns)),
        )
        .filter(col("amount_of_records") > 1)
        .drop("amount_of_records")
        .distinct()
    )

    for column in duplication_columns:
        q.append(f"old.{column} = new.{column}")

    q = " AND ".join(q)

    # Remove all the duplicate records
    delta_table.alias("old").merge(
        duplicate_records.alias("new"), q
    ).whenMatchedDelete().execute()


def drop_duplicates_pkey(
    delta_table: DeltaTable, primary_key: str, duplication_columns: List[str]
):
    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    if not primary_key:
        raise TypeError("A unique primary key must be specified.")

    if not duplication_columns or len(duplication_columns) == 0:
        raise TypeError("A duplication column must be specified.")

    if primary_key in duplication_columns:
        raise TypeError("Primary key must not be part of the duplication columns.")

    data_frame = delta_table.toDF()

    # Make sure that all the required columns are present in the provided delta table
    data_frame_columns = data_frame.columns
    required_columns = [primary_key] + duplication_columns
    for required_column in required_columns:
        if required_column not in data_frame_columns:
            raise TypeError(
                f"The base table has these columns '{data_frame_columns}', but these columns are required '{required_columns}'"
            )

    q = []

    # Get all the duplicate records
    if len(duplication_columns) > 0:
        duplicate_records = (
            data_frame.withColumn(
                "row_number",
                row_number().over(
                    Window().partitionBy(duplication_columns).orderBy(primary_key)
                ),
            )
            .filter(col("row_number") > 1)
            .drop("row_number")
            .distinct()
        )
        for column in required_columns:
            q.append(f"old.{column} = new.{column}")

    else:
        duplicate_records = (
            data_frame.withColumn(
                "row_number",
                row_number().over(
                    Window().partitionBy(primary_key).orderBy(primary_key)
                ),
            )
            .filter(col("row_number") > 1)
            .drop("row_number")
            .distinct()
        )

        for column in duplicate_records.columns + [primary_key]:
            q.append(f"old.{column} = new.{column}")

    q = " AND ".join(q)

    # Remove all the duplicate records
    delta_table.alias("old").merge(
        duplicate_records.alias("new"), q
    ).whenMatchedDelete().execute()


def drop_duplicates(delta_table: DeltaTable, duplication_columns: List[str]):
    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    if not duplication_columns or len(duplication_columns) == 0:
        raise TypeError("A duplication column must be specified.")

    data_frame = delta_table.toDF()

    details = delta_table.detail().select("location").collect()[0]

    (
        data_frame.drop_duplicates(duplication_columns)
        .write.format("delta")
        .mode("overwrite")
        .save(details["location"])
    )


def copy_table(
    delta_table: DeltaTable, target_path: str = None, target_table: str = None
):
    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    if not target_path and not target_table:
        raise TypeError("Either target_path or target_table must be specified.")

    origin_table = delta_table.toDF()

    details = delta_table.detail().select("partitionColumns", "properties").collect()[0]

    if target_table:
        (
            origin_table.write.format("delta")
            .partitionBy(details["partitionColumns"])
            .options(**details["properties"])
            .saveAsTable(target_table)
        )
    else:
        (
            origin_table.write.format("delta")
            .partitionBy(details["partitionColumns"])
            .options(**details["properties"])
            .save(target_path)
        )


def validate_append(
    delta_table: DeltaTable,
    append_data: DataFrame,
    required_cols: List[str],
    optional_additional_cols: List[str],
):
    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    if not isinstance(append_data, DataFrame):
        raise TypeError("You must provide a DataFrame that is to be appended.")

    data_frame_columns = append_data.columns

    for required_column in required_cols:
        if required_column not in data_frame_columns:
            raise TypeError(
                f"The base table has these columns '{data_frame_columns}', but these columns are required '{required_cols}'"
            )

    table_columns = delta_table.toDF().columns

    for column in data_frame_columns:
        if column not in table_columns and column not in optional_additional_cols:
            raise TypeError(
                f"The column '{column}' is not part of the current Delta Table."
                + " If you want to add the column to the table you can use the optional_additional_cols parameter."
            )

    details = delta_table.detail().select("location").collect()[0]

    (
        append_data.write.format("delta")
        .mode("append")
        .option("mergeSchema", "true")
        .save(details["location"])
    )


def append_without_duplicates(
    delta_table: DeltaTable, append_data: DataFrame, p_keys: List[str] = None
):
    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    condition_columns = []
    for column in p_keys:
        condition_columns.append(f"old.{column} = new.{column}")

    condition_columns = " AND ".join(condition_columns)

    # Insert records without duplicates
    delta_table.alias("old").merge(
        append_data.alias("new"), condition_columns
    ).whenNotMatchedInsertAll().execute()


def is_composite_key_candidate(delta_table: DeltaTable, cols: List[str]) -> bool:
    if not isinstance(delta_table, DeltaTable):
        raise TypeError("An existing delta table must be specified.")

    if not cols or len(cols) == 0:
        raise TypeError("At least one column must be specified.")

    data_frame = delta_table.toDF()

    for required_column in cols:
        if required_column not in data_frame.columns:
            raise TypeError(
                f"The base table has these columns '{data_frame.columns}', but these columns are required '{cols}'"
            )

    duplicate_records = (
        data_frame.withColumn(
            "amount_of_records",
            count("*").over(Window.partitionBy(cols)),
        )
        .filter(col("amount_of_records") > 1)
        .drop("amount_of_records")
    )

    if len(duplicate_records.take(1)) == 0:
        return True

    return False


def delta_file_sizes(delta_table: DeltaTable):
    details = delta_table.detail().select("numFiles", "sizeInBytes").collect()[0]
    size_in_bytes, number_of_files = details["sizeInBytes"], details["numFiles"]
    average_file_size_in_bytes = round(size_in_bytes / number_of_files, 0)

    return {
        "size_in_bytes": size_in_bytes,
        "number_of_files": number_of_files,
        "average_file_size_in_bytes": average_file_size_in_bytes,
    }


def humanize_bytes(n: int) -> str:
    for prefix, k in (
        ("PB", 1e15),
        ("TB", 1e12),
        ("GB", 1e9),
        ("MB", 1e6),
        ("kB", 1e3),
    ):
        if n >= k * 0.9:
            return f"{n / k:.2f} {prefix}"
    return f"{n} B"


def find_composite_key_candidates(
    df: Union[DeltaTable, DataFrame], exclude_cols: List[str] = None
):
    if type(df) == DeltaTable:
        df = df.toDF()
    if exclude_cols is None:
        exclude_cols = []
    df_col_excluded = df.drop(*exclude_cols)
    total_cols = len(df_col_excluded.columns)
    total_row_count = df_col_excluded.distinct().count()
    for n in range(1, len(df_col_excluded.columns) + 1):
        for c in combinations(df_col_excluded.columns, n):
            if df_col_excluded.select(*c).distinct().count() == total_row_count:
                if len(df_col_excluded.select(*c).columns) == total_cols:
                    raise ValueError("No composite key candidates could be identified.")
                return list(df_col_excluded.select(*c).columns)


def with_md5_cols(
    df: Union[DeltaTable, DataFrame],
    list_of_columns: List[str],
    uuid_col_name: str = None,
):
    if uuid_col_name is None:
        uuid_col_name = "_".join(["md5"] + list_of_columns)
    if type(df) == DeltaTable:
        df = df.toDF()
    return df.withColumn(uuid_col_name, md5(concat_ws("||", *list_of_columns)))
