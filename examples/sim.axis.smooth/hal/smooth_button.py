"""
Smooth Button Handler for LinuxCNC Axis

This script handles the custom button press and LED feedback in the Axis interface.
"""
import os
import hal
from smooth_linuxcnc.config import read_smooth_config

class SmoothButton:
    def __init__(self, halcomp, builder):
        self.hal = halcomp
        self.ini_path = os.environ.get("AXIS_PROGRESS_BAR", "")
        
        # Create HAL pins
        self.hal.newpin("button-in", hal.HAL_BIT, hal.HAL_IN)
        self.hal.newpin("led-out", hal.HAL_BIT, hal.HAL_OUT)
        
        # Connect to the UI signals
        self.hal.connect("halui.user-enable.0", "button-in")
        self.hal.connect("led-out", "halui.user-led.0")
        
        # Initial state
        self.last_button_state = False
        
        # Set up a timer to check button state (100ms)
        self.hal.timeout_add(100, self.update)
        
        print("Smooth: Button handler initialized")
    
    def sync_tools(self):
        """Handle the tool synchronization."""
        try:
            config = read_smooth_config(self.ini_path)
            print(f"Smooth: Starting tool sync with {config['URL']}")
            # Call your sync script here with config
            # subprocess.Popen(["path/to/sync_script.sh", config['URL'], config['TOKEN']])
            return True
        except Exception as e:
            print(f"Smooth: Error syncing tools: {e}")
            return False
    
    def update(self):
        """Update the button and LED state."""
        try:
            current_state = bool(self.hal["button-in"])
            
            # Detect button press (rising edge)
            if current_state and not self.last_button_state:
                self.hal["led-out"] = True
                self.sync_tools()
            elif not current_state:
                self.hal["led-out"] = False
            
            self.last_button_state = current_state
            return True
        except Exception as e:
            print(f"Smooth: Error in update: {e}")
            return False

def get_handlers(halcomp, builder):
    """Return the list of handlers for Axis to use."""
    return [SmoothButton(halcomp, builder)]
