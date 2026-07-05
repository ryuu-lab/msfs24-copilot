import sys
print("Python version:", sys.version)
print("Python path:", sys.executable)

# Test tkinter
try:
    import tkinter
    print("✓ tkinter available")
except ImportError as e:
    print("✗ tkinter missing:", e)

# Test openai
try:
    import openai
    print("✓ openai available")
except ImportError as e:
    print("✗ openai missing:", e)

# Test dotenv
try:
    from dotenv import load_dotenv
    print("✓ dotenv available")
except ImportError as e:
    print("✗ dotenv missing:", e)

# Test .env file and API key
import os
from dotenv import load_dotenv
load_dotenv()
key = os.getenv("DEEPSEEK_API_KEY")
if key:
    print("✓ API key found:", key[:10] + "...")
else:
    print("✗ No API key - create .env file with DEEPSEEK_API_KEY=sk-...")

input("\nPress Enter to exit...")