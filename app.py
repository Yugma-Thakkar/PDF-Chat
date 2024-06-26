import os
import time
import langchain
from openai import OpenAI
import anthropic
import streamlit as st
import pickle
from streamlit_extras.add_vertical_space import add_vertical_space
from PyPDF2 import PdfReader
from langchain.prompts import PromptTemplate
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings.huggingface import HuggingFaceEmbeddings
from langchain.retrievers import ContextualCompressionRetriever
from langchain.retrievers.document_compressors import LLMChainExtractor
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain.chains.question_answering import load_qa_chain
from langchain.chains.question_answering import _load_stuff_chain
from langchain.chains.conversation.memory import ConversationBufferWindowMemory
from langchain.chains.conversational_retrieval.base import ConversationalRetrievalChain

# langchain.debug = True

# number of chunks to return from the PDF
ret_chunks = 5

# Extract text from a PDF file.
def extract_text(pdf):
    reader = PdfReader(pdf)
    text = ""
    for page in reader.pages:
        text += page.extract_text()
    return text

def split_text(text):
    """Split the text into chunks."""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=200,
        length_function=len
    )
    return splitter.split_text(text=text)

# EMBEDDING MODELS
embedding_model = 'sentence-transformers/sentence-t5-base' # okayish, not that good
# embedding_model = 'sentence-transformers/msmarco-distilbert-base-dot-prod-v3' # better than the above

@st.cache_data(show_spinner=False)
def openai_api_key_test(api_key):
    # Test the OpenAI API key
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "This is an authentication test."},
                {"role": "user", "content": "Return True if you can see this message."},
            ],
            max_tokens=5,
            temperature=0.0
        )
        if response.choices[0].message.content == "True": return True
    except: return False

@st.cache_data(show_spinner=False)
def anthropic_api_key_test(api_key):
    # Test the Anthropic API key.
    try:
        client = anthropic.Client(api_key=api_key)
        response = client.messages.create(
            model="claude-3-haiku-20240307",
            system="This is an authentication test.",
            messages=[
                {"role": "user", "content": "Return True if you can see this message."}
            ],
            max_tokens=5,
            temperature=0.0
        )
        if response.content[0].text == "True": return True
    except: return False

def clear_history():
    # Clear chat
    if st.button("Clear chat history"):
        st.session_state.messages = []
        st.rerun()

# Get user inputs from the Streamlit sidebar.
def get_user_inputs():
    with st.sidebar:
        st.write("# PDF Chatbot")

        model_type = st.selectbox(
            label="Please select your preferred AI model to get started:",
            placeholder="Select your preferred AI model",
            options=["OpenAI", "Anthropic"],
            label_visibility="visible",
            index=None
        )

        model = None
        if model_type == "OpenAI":
            model = st.radio("Select your preferred OpenAI model", ["gpt-3.5-turbo", "gpt-4", "gpt-4-turbo"], index=None)
        elif model_type == "Anthropic":
            model = st.radio("Select your preferred Anthropic model", ["claude-3-opus-20240229", "claude-3-sonnet-20240229", "claude-3-haiku-20240307"], index=None)

        add_vertical_space(1)

        api_key = None
        temperature = 0
        if model is not None and model_type is not None:
            api_key = st.text_input(f"Please enter your {model_type} API Key here:")
            add_vertical_space(1)
            if api_key is not None and temperature is not None:
                temperature = st.select_slider("Select the temperature for the model", options=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0], value=0.0)

        st.divider()
        st.markdown('''
        ## Instructions
        1. Select your preferred AI model
        2. Upload your API key
        3. Upload your PDF file
        4. Ask your question
        5. Get the answer
        6. Repeat!
        ''')
        st.divider()
        add_vertical_space(3)
        st.write("This app was created using [Streamlit](https://streamlit.io/), [Langchain](https://langchain.com/), [Claude](https://www.anthropic.com/) and [OpenAI](https://openai.com/).")

    return model_type, model, api_key, temperature

# Create or load a vector store from the text chunks.
def create_vector_store(bar, chunks, store_name):
    if os.path.exists(f"{store_name}.pkl"):
        if bar is not None:
            bar.progress(0.5, text="Loading text chunks...")
        with open(f"{store_name}.pkl", "rb") as f:
            vector_store = pickle.load(f)
    else:
        embeddings = HuggingFaceEmbeddings(model_name=embedding_model)
        bar.progress(0.35, text="Embedding text chunks...")
        vector_store = FAISS.from_texts(chunks, embedding=embeddings)
        with open(f"{store_name}.pkl", "wb") as f:
            pickle.dump(vector_store, f)
    return vector_store

def process_pdf_file(pdf):
    if pdf != st.session_state.get('last_processed_pdf', None):
        st.session_state['last_processed_pdf'] = pdf  # update the session state
            
        bar = st.progress(0, text="Extracting text from the PDF...")
        text = extract_text(pdf)

        bar.progress(0.25, text="Splitting text into chunks...")
        chunks = split_text(text)
        
        store_name = pdf.name[:-4]
        vector_store = create_vector_store(bar=bar, chunks=chunks, store_name=store_name)
        st.session_state['vector_store'] = vector_store
        
        bar.progress(1.0, text="Text chunks loaded.")
        time.sleep(0.5)
        bar.empty()
        return vector_store
    elif pdf == st.session_state.get('last_processed_pdf', None):
        return st.session_state['vector_store']
    else:
        text = extract_text(pdf)
        chunks = split_text(text)
        store_name = pdf.name[:-4]
        vector_store = create_vector_store(bar=None, chunks=chunks, store_name=store_name)
        st.session_state['vector_store'] = vector_store
        return vector_store

def get_answer(pdf, question, model_type, model, api_key, temperature):
    vector_store = process_pdf_file(pdf)
    if vector_store is not None:
        if model_type == "OpenAI":
            chat = ChatOpenAI(openai_api_key=api_key, model=model, temperature=temperature)
        elif model_type == "Anthropic":
            chat = ChatAnthropic(anthropic_api_key=api_key, model=model, temperature=temperature)

        # Remove the oldest message if the chat history is too long
        if len(st.session_state.messages) >= 3:
            st.session_state.messages.pop(0)

        with st.spinner("Thinking..."):
            compressor = LLMChainExtractor.from_llm(chat)
            compression_retriever = ContextualCompressionRetriever(
                base_compressor=compressor, base_retriever=vector_store.as_retriever(search_type="similarity", search_kwargs={"k": ret_chunks})
            )

            template = """As a friendly chatbot assistant, your goal is to provide the user accurate answers for their question using the given context and previous conversation. If unsure, it's okay to admit not knowing. Avoid inventing answers. Aim to preserve the context's language when responding. Feel free to add relevant information, indicating it's not in the original context. Use friendly (but professional) and non-technical language unless required due to the user's question or the nature of the context.
            
            {context}

            Question: {question}

            Chat history: {chat_history}
            
            Helpful Answer:"""
            qa_chain_prompt = PromptTemplate.from_template(template)

            # OTHER RETRIEVERS
            # docs = vector_store.similarity_search(query=question, k=ret_chunks)
            # docs = vector_store.max_marginal_relevance_search(query=question, k=ret_chunks, fetch_k=10)
            docs = compression_retriever.get_relevant_documents(query=question)
            # st.write(f"Most relevant chunks for the question '{question}':")
            # for i, doc in enumerate(docs):
            #     st.write(f"{i+1}. {doc}")

            interactions = []

            # chain = load_qa_chain(llm=chat, chain_type="stuff")
            chain = _load_stuff_chain(llm=chat, prompt=qa_chain_prompt)
            response = chain.run(input_documents=docs, question=question, chat_history=st.session_state.messages, verbose=True)
            # AI response
            st.chat_message(name="ai").write(response)

            # Add this interaction to interactions
            interactions.append([{"name": "user", "message": question}, {"name": "ai", "message": response}])

            # Update chat history
            st.session_state.messages.append(interactions)

def main():
    st.set_page_config(page_title="PDF Chatbot", page_icon="🤖")
    st.title("PDF Chatbot 💬")

    model_type, model, api_key, temperature = get_user_inputs()

    # Initialize chat history   
    if "messages" not in st.session_state:
        st.session_state.messages = []

    if api_key:
        if model_type == "OpenAI":
            with st.spinner("Testing the OpenAI API key..."):
                test_return = openai_api_key_test(api_key)
        elif model_type == "Anthropic":
            with st.spinner("Testing the Anthropic API key..."):
                test_return = anthropic_api_key_test(api_key)

        if test_return == True:
            pdf = st.file_uploader("Please upload a PDF file to get started", type="pdf")

            if pdf is not None:
                process_pdf_file(pdf)
                clear_history()

                # Chat input
                question = st.chat_input("Ask a question:")
                # if question is not None:
                #     # User input
                #     st.chat_message(name="user").write(question)

                # Display chat history from history on app rerun
                for chat in st.session_state.messages:
                    for interaction in chat:
                        st.chat_message(name=interaction[0]["name"]).write(interaction[0]["message"])
                        st.chat_message(name=interaction[1]["name"]).write(interaction[1]["message"])
                
                if question:
                    st.chat_message(name="user").write(question)
                    get_answer(pdf, question, model_type, model, api_key, temperature)
                
                # print(st.session_state.messages)
                # st.write(len(st.session_state.messages))
                # print(st.session_state.messages)

if __name__ == "__main__":
    main()
