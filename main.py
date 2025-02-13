from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any
import sqlite3
import os
from dotenv import load_dotenv
from langchain.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from typing_extensions import Annotated, TypedDict
from langchain_community.utilities import SQLDatabase
from langgraph.graph import StateGraph, START
from langchain import hub

# Additional imports for the new QnA pipeline:
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import create_react_agent

# Load environment variables (e.g., GROQ_API_KEY)
load_dotenv()

# Initialize FastAPI
app = FastAPI()

# --- Replicate Notebook Logic ---

# Initialize Groq LLM
llm = ChatGroq(
    temperature=0,
    model="llama-3.3-70b-versatile",
    api_key=os.getenv("GROQ_API_KEY_2")
)

# --- Classification Setup ---
class ClassifyInput(TypedDict):
    input_type: Annotated[str, "Only takes values: 'ingestion' or 'chat'"]

classify_prompt = ChatPromptTemplate.from_messages([
    ("system", 
     "Classify the user query as either **'ingestion'** or **'chat'**.\n\n"
     "**Examples:**\n\n"
     "1️⃣ Ingestion:\n"
     "   - 'I spent 500 at McDonald's yesterday'\n"
     "   - 'Paid 2000 for rent on 2024-02-01'\n"
     "   - 'Added a transaction of 1500 for groceries on Feb 10'\n\n"
     "2️⃣ Chat:\n"
     "   - 'What was my highest expense last month?'\n"
     "   - 'Show all transactions for February'\n"
     "   - 'How much did I spend on Amazon?'\n\n"
     "If the query is **providing transaction details**, return 'ingestion'.\n"
     "If the query is **asking for information**, return 'chat'.\n\n"
     "Respond with ONLY 'ingestion' or 'chat'."),
    
    ("human", "Query: {chunk}\n\nClassify the query as either 'ingestion' or 'chat'.")
])
classify_llm = llm.with_structured_output(ClassifyInput)

def classify_query(chunk: str) -> str:
    response = classify_llm.invoke(classify_prompt.format(chunk=chunk))
    return response["input_type"]

# --- Ingestion Pipeline Setup ---
class Transaction(TypedDict):
    date: Annotated[str, "The date of the transaction in 'YYYY-MM-DD' format."]
    amount: Annotated[float, "The amount spent in the transaction."]
    vendor: Annotated[str, "The name of the vendor or store where the transaction occurred."]
    description: Annotated[str, "A brief description of the transaction, such as items purchased."]

transaction_prompt = ChatPromptTemplate.from_messages([
    ("system", "Extract the transaction details from the given text input. Focus on date, amount, vendor, and description."),
    ("human", "Text: {chunk}\n\nExtract the following details:\n- Date\n- Amount\n- Vendor\n- Description.")
])
transaction_llm = llm.with_structured_output(Transaction)

def extract_transaction_details(chunk: str) -> Dict[str, Any]:
    response = transaction_llm.invoke(transaction_prompt.format(chunk=chunk))
    return dict(response)

def create_transaction_table():
    conn = sqlite3.connect('transactions.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            amount REAL,
            vendor TEXT,
            description TEXT
        )
    ''')
    conn.commit()
    conn.close()

def insert_transaction_to_db(transaction: Dict[str, Any]):
    conn = sqlite3.connect('transactions.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO transactions (date, amount, vendor, description)
        VALUES (?, ?, ?, ?)
    ''', (
        transaction['date'],
        transaction['amount'],
        transaction['vendor'],
        transaction['description']
    ))
    conn.commit()
    conn.close()

def process_and_store_transaction(chunk: str):
    transaction = extract_transaction_details(chunk)
    insert_transaction_to_db(transaction)

# --- QnA Pipeline Setup ---
# Create the SQLDatabase object for the transactions database.
db = SQLDatabase.from_uri("sqlite:///transactions.db")

# Define a new system prompt without placeholders for dialect or limits.
system_message = """
Given an input question, create a syntactically correct SQL query to run to help find the answer.
Do not apply any default limit on the number of results.
Order the results by a relevant column to return the most interesting examples in the database.

Never query for all the columns from a specific table; only ask for the few relevant columns given the question.

Pay attention to use only the column names that you can see in the schema description.
Be careful not to query for columns that do not exist.
Also, pay attention to which column is in which table.
"""

# Build the SQL agent using the SQLDatabaseToolkit.
toolkit = SQLDatabaseToolkit(db=db, llm=llm)
tools = toolkit.get_tools()
agent_executor = create_react_agent(llm, tools, prompt=system_message)

# --- FastAPI Endpoints ---
class TextInput(BaseModel):
    text: str

@app.on_event("startup")
def startup():
    create_transaction_table()

@app.post("/process")
async def process_text(input: TextInput):
    input_text = input.text
    classification = classify_query(input_text)
    
    if classification == 'ingestion':
        try:
            process_and_store_transaction(input_text)
            return {
                "status": "success",
                "type": "ingestion",
                "message": "Data added successfully."
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    else:
        try:
            # Use the new RAG pipeline with create_react_agent.
            # Incorporate the provided snippet for processing the QnA query.
            question = input_text
            final_result = None
            for chunk in agent_executor.stream(
                {"messages": [{"role": "user", "content": question}]},
                stream_mode="values"
            ):
                final_result = chunk
            result = final_result["messages"][-1].content if final_result else "No answer found."
            return {
                "status": "success",
                "type": "qna",
                "answer": result
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
