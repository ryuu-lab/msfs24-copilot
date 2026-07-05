# save as 
import keyboard
import time

print("Testing keyboard simulation...")
print("Make sure Notepad or a text field is open and focused!")
print("You have 5 seconds to click into a text editor...")
time.sleep(5)

# Type something
keyboard.press_and_release("g")
print("Sent 'g' key - did it appear in your text editor?")

time.sleep(1)
keyboard.press_and_release("f5")
print("Sent 'f5' - did it do something?")

time.sleep(1)
keyboard.press_and_release("ctrl+.")
print("Sent 'ctrl+.' - parking brake combo")