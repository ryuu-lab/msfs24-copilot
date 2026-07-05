# save as test_voice.py
import pyttsx3
import time

engine = pyttsx3.init()
engine.setProperty('rate', 170)
engine.setProperty('volume', 1.0)

# List available voices
voices = engine.getProperty('voices')
print("Available voices:")
for i, v in enumerate(voices):
    print(f"  {i}: {v.name}")

# Test speaking
print("\nTesting voice...")
engine.say("Gear up. Flaps set. 80 knots. V1. Rotate. Positive rate.")
engine.runAndWait()
print("Done! Did you hear all of that?")