# save as test_queue.py
import pyttsx3
import queue
import threading
import time

voice_queue = queue.Queue()

def voice_worker():
    local_engine = pyttsx3.init()
    local_engine.setProperty('rate', 150)
    local_engine.setProperty('volume', 1.0)
    print("Voice worker started")
    
    while True:
        try:
            text = voice_queue.get(timeout=1)
            if text:
                print(f"Speaking: {text}")
                local_engine.say(text)
                local_engine.runAndWait()
                print(f"Done: {text}")
        except queue.Empty:
            pass

threading.Thread(target=voice_worker, daemon=True).start()

# Test multiple items
time.sleep(1)  # Wait for worker to initialize

print("Adding items to queue...")
voice_queue.put("Gear up.")
time.sleep(0.1)
voice_queue.put("Flaps set.")
time.sleep(0.1)
voice_queue.put("80 knots.")
time.sleep(0.1)
voice_queue.put("V1.")
time.sleep(0.1)
voice_queue.put("Rotate.")
time.sleep(0.1)
voice_queue.put("Positive rate.")

print("All items queued. Waiting...")
time.sleep(10)  # Give time for all to play
print("Test complete.")