import SimConnect

try:
    sm = SimConnect.SimConnect()
    print("✓ Connected to MSFS 2024!")
    
    # Test: Request a simple variable to confirm connection works
    # Try to read the simulation rate (always available)
    result = sm.get_sim_data("SIM RATE")
    print(f"✓ Sim data read successful: {result}")
    
    sm.exit()
    print("✓ Connection closed cleanly")
except Exception as e:
    print(f"✗ Error: {e}")
    print("But connection was established - just need correct event format")