from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response  # Import Response to set the media type
from fastapi.responses import PlainTextResponse
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

# Import Twilio's MessagingResponse for sending back TwiML responses
from twilio.twiml.messaging_response import MessagingResponse

# Load environment variables (e.g., GROQ_API_KEY)
load_dotenv()

# Initialize FastAPI
app = FastAPI()

# --- Replicate Notebook Logic ---

llm = ChatGroq(
    temperature=0,
    model="llama-3.3-70b-versatile",
    api_key=os.getenv("GROQ_API_KEY_3")
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
from typing import Annotated
from typing_extensions import TypedDict

class Transaction(TypedDict):
    transaction_id: Annotated[int, "Unique identifier for the transaction."]
    date: Annotated[str, "The date of the transaction in 'YYYY-MM-DD' format."]
    amount: Annotated[float, "The amount spent in the transaction."]
    vendor: Annotated[str, "The name of the vendor or store where the transaction occurred."]
    description: Annotated[str, "A brief description of the transaction, such as items purchased."]
    category: Annotated[str, "The category of the transaction (e.g., food, groceries, entertainment)."]
    payment_method: Annotated[str, "The payment method used for the transaction (e.g., credit card, cash, online payment)."]
    location: Annotated[str, "The location where the transaction occurred (if available)."]
    notes: Annotated[str, "Any additional notes related to the transaction."]
    currency: Annotated[str, "The currency used for the transaction (e.g., INR, USD, EUR)."]


transaction_prompt = ChatPromptTemplate.from_messages([
    ("system", """
        Extract the following transaction details from the given text input. 
        Focus on transaction_id, date, amount, vendor, description, category, 
        payment_method, location, notes, and currency. 
        Ensure that each field is correctly identified and extracted.
    """),
    ("human", """
        Text: {chunk}

        Extract the following details:
        - Transaction ID (optional, if available)
        - Date (in 'YYYY-MM-DD' format)
        - Amount (in numeric form)
        - Vendor (store or vendor name)
        - Description (brief description of the transaction)
        - Category (e.g., food, electronics, groceries)
        - Payment Method (e.g., credit card, cash, online payment)
        - Location (e.g., city, online, specific store)
        - Notes (any additional information related to the transaction)
        - Currency (e.g., INR, USD, EUR)
    """)
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
query_prompt_template = hub.pull("langchain-ai/sql-agent-system-prompt")
system_message = query_prompt_template.format(dialect="SQLite", top_k="")

# Build the SQL agent using the SQLDatabaseToolkit.
toolkit = SQLDatabaseToolkit(db=db, llm=llm)
tools = toolkit.get_tools()
agent_executor = create_react_agent(llm, tools, prompt=system_message)

# --- Regular FastAPI Endpoint for JSON-based Requests ---
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
            # QnA pipeline using the React agent
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


