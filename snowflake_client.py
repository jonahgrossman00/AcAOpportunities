import os

import pandas as pd
import snowflake.connector
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return snowflake.connector.connect(
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE", "Salesforce"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "SFDC"),
        role=os.getenv("SNOWFLAKE_ROLE"),
    )


def fetch_opportunities(limit=None):
    query = "SELECT * FROM Salesforce.SFDC.Opportunity"
    if limit:
        query += f" LIMIT {limit}"

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(query)
        return cursor.fetch_pandas_all()
    finally:
        conn.close()


if __name__ == "__main__":
    df = fetch_opportunities(limit=100)
    print(df.shape)
    print(df.head())

    df.to_csv("opportunities.csv", index=False)
    print("Saved to opportunities.csv")
