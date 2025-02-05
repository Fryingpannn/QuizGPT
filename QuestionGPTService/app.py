import os
import json
import pika
import requests
from flask import Flask
import threading
from utils import MOCK_RESPONSE, gpt_prompt, is_approved_sender
import logging

app = Flask(__name__)
env = os.environ["ENVIRONMENT"]

logging.basicConfig(level=logging.DEBUG)

# RabbitMQ configurations
RABBITMQ_HOST = "localhost" if env == "dev" else os.environ["RABBITMQ_HOSTNAME"]
INPUT_QUEUE = os.environ.get("INPUT_QUEUE", "input_queue")
OUTPUT_QUEUE = os.environ.get("OUTPUT_QUEUE", "output_queue")
RABBITMQ_USER = "guest" if env == "dev" else os.environ["RABBITMQ_USER"]
RABBITMQ_PASSWORD = "guest" if env == "dev" else os.environ["RABBITMQ_PASSWORD"]
RABBITMQ_PORT = int(os.environ["RABBITMQ_PORT"])

if env == "dev":
    print("RABBITMQ_HOST: ", RABBITMQ_HOST)
    print("INPUT_QUEUE: ", INPUT_QUEUE)
    print("OUTPUT_QUEUE: ", OUTPUT_QUEUE)
    print("RABBITMQ_USER: ", RABBITMQ_USER)
    print("RABBITMQ_PASSWORD: ", RABBITMQ_PASSWORD)
    print("RABBITMQ_PORT: ", RABBITMQ_PORT)


# ChatGPT API configurations
CHATGPT_API_KEY = os.environ.get("OPENAI_SECRET_KEY")
CHATGPT_API_ENDPOINT = os.environ.get("CHATGPT_API_ENDPOINT", "https://api.openai.com/v1/chat/completions")

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {CHATGPT_API_KEY}"
}

# RabbitMQ singleton channel
global GLOBAL_RMQ_CHANNEL
GLOBAL_RMQ_CHANNEL = None

def chatgpt_request(prompt, nb_questions, max_tokens=200):
    try:
        final_prompt = gpt_prompt(prompt, nb_questions)
        data = {
            "model": "gpt-3.5-turbo-0301",
            "messages": [
                {"role": "user", "content": final_prompt}
            ],
            "max_tokens": max_tokens,
            "n": 1,
            "temperature": 0.8,
        }

        if env == "dev":
            return MOCK_RESPONSE
        
        response = requests.post(CHATGPT_API_ENDPOINT, headers=headers, json=data, timeout=7)
        print("Response: ", response.json())
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print("Error occured while sending request to ChatGPT API: ", e)

def parse_qa_pairs(text):
    questions = text.split("\n\n")
    formatted_questions = []

    for question in questions:
        split_question = question.split("\n")
        question_text = split_question[0].strip()
        answer = split_question[1].strip()

        formatted_questions.append({"question": question_text, "answer": answer})
    return formatted_questions

def callback(ch, method, properties, body):
    try:
        message = json.loads(body)
        print(message)
        quiz_id = message["conversationId"]
        number = message.get("number",0)
        text = message.get("text","")
        sender = message.get("sender","")
        
        if not is_approved_sender(sender):
            print('Sender not approved. Ignoring message.')
            return

        generated_text = chatgpt_request(text, number)
        if generated_text is None:
            print("Error generating text.")
            return
        print('Generated text---\n',generated_text)
        # Parse questions and answers
        formatted_qa = parse_qa_pairs(generated_text)

        # Add message back to the queue
        response_message = {
            "conversationId": quiz_id,
            "number": number,
            "results": formatted_qa
        }
        print('Response message---\n',response_message)

        ch.basic_publish(exchange="", routing_key=OUTPUT_QUEUE, body=json.dumps(response_message))
        ch.basic_ack(delivery_tag=method.delivery_tag)
        print('Finished callback')
    except Exception as e:
        print("Error processing message: ", e)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

def start_consuming():
    try:
        global GLOBAL_RMQ_CHANNEL
        print("Starting RabbitMQ connection.")
        if env == "dev":
            print("Using dev RabbitMQ connection.")
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST))
        else:
            print("Using prod RabbitMQ connection.")
            credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
            connection = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST, port=RABBITMQ_PORT, virtual_host=RABBITMQ_USER, credentials=credentials))
        print("Testing connection...")
        if connection.is_open:
            print(f"Successfully connected to RabbitMQ at {RABBITMQ_HOST}")
        else:
            print(f"Failed to connect to RabbitMQ at {RABBITMQ_HOST}")
            return None
        print("Creating channel")
        GLOBAL_RMQ_CHANNEL = connection.channel()
        print("Creating queues")
        GLOBAL_RMQ_CHANNEL.queue_declare(queue=INPUT_QUEUE)
        GLOBAL_RMQ_CHANNEL.queue_declare(queue=OUTPUT_QUEUE)
        if GLOBAL_RMQ_CHANNEL is None:
            print("Error connecting to input queue. Exiting...")
            return "Channel is None"
        else: print('RabbitMQ connection established and channel created. Starting to consume...')
        GLOBAL_RMQ_CHANNEL.basic_qos(prefetch_count=1)
        GLOBAL_RMQ_CHANNEL.basic_consume(queue=INPUT_QUEUE, on_message_callback=callback)
        GLOBAL_RMQ_CHANNEL.start_consuming()
        return "Consuming..."
    except Exception as e:
        print('Error occured during consuming')
        print(e)


@app.route("/test")
def test():
    try:
        test_prompt = 'Algorithm and data structures. Medium difficulty.'
        response = chatgpt_request(test_prompt, 2)
        print(response)
        finalr = parse_qa_pairs(response)
        print(finalr)
        return json.dumps([finalr])
    except Exception as e:
        print(e)
        return json.dumps(['Error occured'])

@app.route('/outputq')
def list_outputq():
    try:
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
        temp_connect = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST, virtual_host=RABBITMQ_USER, credentials=credentials))
        temp_channel = temp_connect.channel()
        temp_channel.queue_declare(queue=OUTPUT_QUEUE)
        msgs = []
        while True:
            method_frame, header_frame, body = temp_channel.basic_get(queue=OUTPUT_QUEUE, auto_ack=False)
            if method_frame:
                msgs.append(json.loads(body))
            else: break
        temp_channel.cancel()
        temp_channel.close()
        return json.dumps(msgs)
    except Exception as e:
        print(e)
        return json.dumps(['Error occured'])

@app.route('/inputq')
def list_inputq():
    try:
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASSWORD)
        temp_connect = pika.BlockingConnection(pika.ConnectionParameters(host=RABBITMQ_HOST, virtual_host=RABBITMQ_USER, credentials=credentials))
        temp_channel = temp_connect.channel()
        temp_channel.queue_declare(queue=INPUT_QUEUE)
        msgs = []
        while True:
            method_frame, header_frame, body = temp_channel.basic_get(queue=INPUT_QUEUE, auto_ack=False)
            if method_frame:
                msgs.append(json.loads(body))
            else: break
        temp_channel.cancel()
        temp_channel.close()
        return json.dumps(msgs)
    except Exception as e:
        print(e)
        return json.dumps(['Error occured'])

@app.route("/forcestart")
def force_start():
    try:
        err = start_consuming()
        return f"Started consuming. Error?: {err}"
    except Exception as e:
        print(e)
        return json.dumps(['Error occured'])

@app.route("/")
def home():
    return "<h1>Question GPT Service is now running.</h1>"

# Go to root home page to start the connection with rabbitmq
# @app.before_first_request
# def startup():
#     print('Starting RabbitMQ thread')
#     rabbitmq_thread = threading.Thread(target=start_consuming)
#     rabbitmq_thread.start()
print('Starting Flask app. Environment: ', env)
print('Starting RabbitMQ thread')
app.logger.info('Logging test')
rabbitmq_thread = threading.Thread(target=start_consuming)
rabbitmq_thread.start()
if __name__ == "__main__":
    if env == "dev":
        print("In dev environment.")
        app.run(debug=True)
