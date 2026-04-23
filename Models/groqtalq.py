import os
from groq import Groq

client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
completion = client.chat.completions.create(
    model="qwen/qwen3-32b",
    messages=[
      {
        "role": "system",
        "content": "Give me 10-20 items that all belong to a specific category.\nRepeat this process 5 times. Shorten each individual item to one word, even if it is not the exact name. \n Output in this format:\n '(well formatted category name):option1,option2,option3'"
      },
      {
        "role": "user",
        "content": "Make them internet related"
      }
    ],
    temperature=0.9,
    max_completion_tokens=4096,
    top_p=0.95,
    reasoning_effort="default",
    stream=True,
    stop=None
)

for chunk in completion:
    print(chunk.choices[0].delta.content or "", end="")
