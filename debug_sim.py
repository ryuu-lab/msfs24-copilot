import SimConnect

try:
    sm = SimConnect.SimConnect()
    print("✓ Connected")
    
    # Use map_to_sim_event to get proper event object
    event = sm.map_to_sim_event("GEAR_TOGGLE")
    print(f"Mapped event: {event} (type: {type(event)})")
    
    sm.send_event(event)
    print("✓ GEAR_TOGGLE sent - check your gear in MSFS!")
    
    sm.exit()
except Exception as e:
    print(f"Error: {e}")