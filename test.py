import requests

api_url = "https://api.deepai.org/api/summarization"
headers = {
    'api-key': '02da7161-9571-4687-9008-3aeabb9f06db'
}

data = {
    'text': '''Machine learning is a branch of artificial intelligence (AI) focused on building applications that learn from data and improve their accuracy over time without being programmed to do so. In data science, an algorithm is a sequence of statistical processing steps.'''
}

response = requests.post(api_url, headers=headers, data=data)

print("Summary:")
print(response.json()['output'])
