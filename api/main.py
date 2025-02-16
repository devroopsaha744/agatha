from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sqlite3
import os
from typing import TypedDict
from langchain_groq import ChatGroq
from langchain.prompts import ChatPromptTemplate
from langchain.chains import create_sql_query_chain
from langchain_community.utilities import SQLDatabase
from langchain_community.tools.sql_database.tool import QuerySQLDatabaseTool
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
load_dotenv()

app = FastAPI(title="Agatha Financial Assistant API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DATABASE_URL = "sqlite:///transactions.db"
DB_FILE = "transactions.db"

llm = ChatGroq(temperature=0, model="llama-3.3-70b-versatile", api_key=GROQ_API_KEY)

def create_transaction_table():
    conn = sqlite3.connect(DB_FILE)
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

@app.on_event("startup")
def on_startup():
    create_transaction_table()

class ClassifyRequest(BaseModel):
    text: str

class TransactionCreate(BaseModel):
    date: str
    amount: float
    vendor: str
    description: str

class QuestionRequest(BaseModel):
    question: str

class ProcessRequest(BaseModel):
    text: str

@app.get("/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}

@app.get("/db_status")
async def database_status():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()
        conn.close()
        return {
            "database": DB_FILE,
            "tables": [table[0] for table in tables],
            "table_count": len(tables)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/classify")
async def classify_input(request: ClassifyRequest):
    try:
        class ClassifyInput(TypedDict):
            input_type: str

        classify_llm = llm.with_structured_output(ClassifyInput)
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Classify the following query as either 'ingestion' or 'chat'."),
            ("human", f"Query: {request.text}\n\nClassify the query as either 'ingestion' or 'chat'.")
        ])
        response = classify_llm.invoke(prompt.format(chunk=request.text))
        return {"input_type": response['input_type']}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ingest")
async def ingest_transaction(request: ClassifyRequest):
    try:
        class Transaction(TypedDict):
            date: str
            amount: float
            vendor: str
            description: str

        transaction_llm = llm.with_structured_output(Transaction)
        prompt = ChatPromptTemplate.from_messages([
            ("system", "Extract transaction details from text"),
            ("human", f"Text: {request.text}\n\nExtract: date, amount, vendor, description")
        ])
        response = transaction_llm.invoke(prompt.format(chunk=request.text))
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO transactions (date, amount, vendor, description)
            VALUES (?, ?, ?, ?)
        ''', (response['date'], response['amount'], response['vendor'], response['description']))
        conn.commit()
        conn.close()
        return {"status": "success", "transaction": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ask")
async def answer_question(request: QuestionRequest):
    try:
        db = SQLDatabase.from_uri(DATABASE_URL)
        generate_query = create_sql_query_chain(llm, db)
        generated_response = generate_query.invoke({"question": request.question})
        query_text = generated_response.split("SQLQuery:")[1].strip()
        execute_query = QuerySQLDatabaseTool(db=db)
        sql_result = execute_query.invoke(query_text)
        answer_prompt = PromptTemplate.from_template(
            "Question: {question}\nSQL Query: {query}\nSQL Result: {result}\nAnswer:"
        )
        rephrase_answer = answer_prompt | llm | StrOutputParser()
        final_answer = rephrase_answer.invoke({
            "question": request.question,
            "query": query_text,
            "result": sql_result
        })
        return {"question": request.question, "answer": final_answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/process")
async def process_query(request: ProcessRequest):
    try:
        class ClassifyInput(TypedDict):
            input_type: str

        classify_llm = llm.with_structured_output(ClassifyInput)
        classification_prompt = ChatPromptTemplate.from_messages([
            ("system", "Classify the following query as either 'ingestion' or 'chat'."),
            ("human", f"Query: {request.text}\n\nClassify the query as either 'ingestion' or 'chat'.")
        ])
        classification_response = classify_llm.invoke(classification_prompt.format(chunk=request.text))
        input_type = classification_response['input_type'].strip().lower()

        if input_type == "ingestion":
            class Transaction(TypedDict):
                date: str
                amount: float
                vendor: str
                description: str

            transaction_llm = llm.with_structured_output(Transaction)
            ingestion_prompt = ChatPromptTemplate.from_messages([
                ("system", "Extract transaction details from text"),
                ("human", f"Text: {request.text}\n\nExtract: date, amount, vendor, description")
            ])
            ingestion_response = transaction_llm.invoke(ingestion_prompt.format(chunk=request.text))
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO transactions (date, amount, vendor, description)
                VALUES (?, ?, ?, ?)
            ''', (ingestion_response['date'], ingestion_response['amount'], ingestion_response['vendor'], ingestion_response['description']))
            conn.commit()
            conn.close()
            return {"input_type": "ingestion", "transaction": ingestion_response}

        elif input_type == "chat":
            db = SQLDatabase.from_uri(DATABASE_URL)
            generate_query = create_sql_query_chain(llm, db)
            generated_response = generate_query.invoke({"question": request.text})
            query_text = generated_response.split("SQLQuery:")[1].strip()
            execute_query = QuerySQLDatabaseTool(db=db)
            sql_result = execute_query.invoke(query_text)
            answer_prompt = PromptTemplate.from_template(
                "Question: {question}\nSQL Query: {query}\nSQL Result: {result}\nAnswer:"
            )
            rephrase_answer = answer_prompt | llm | StrOutputParser()
            final_answer = rephrase_answer.invoke({
                "question": request.text,
                "query": query_text,
                "result": sql_result
            })
            return {"input_type": "chat", "question": request.text, "answer": final_answer}
        else:
            raise HTTPException(status_code=400, detail="Unknown classification result")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
