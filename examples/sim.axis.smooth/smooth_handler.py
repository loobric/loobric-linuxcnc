import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk
import requests
import configparser
import os

class HandlerClass:
    def __init__(self, halcomp, builder):
        self.hal = halcomp
        self.builder = builder
        self.led_on = False
        
        # Get the LED label
        self.led = self.builder.get_object('led')
        
        # Read configuration from axis.ini
        self.config = configparser.ConfigParser(strict=False)
        config_path = os.path.join(os.path.dirname(__file__), 'axis.ini')
        self.config.read(config_path)
        
        self.server_url = self.config.get('SMOOTH', 'SERVER_URL', fallback='')
        self.token = self.config.get('SMOOTH', 'TOKEN', fallback='')
        
        # Set initial LED state
        self.update_led()
    
    def check_server_connectivity(self):
        """Check server connectivity by making a health request"""
        try:
            headers = {'Authorization': f'Bearer {self.token}'} if self.token else {}
            response = requests.get(f'{self.server_url}/api/health', headers=headers, timeout=5)
            return response.status_code == 200
        except:
            return False
    
    def update_led(self):
        print(f"[DEBUG] update_led called, led_on={self.led_on}")
        if self.led_on:
            self.led.set_text("ON")
            color = Gdk.RGBA()
            color.parse('green')
            self.led.override_background_color(Gtk.StateFlags.NORMAL, color)
        else:
            self.led.set_text("OFF")
            color = Gdk.RGBA()
            color.parse('red')
            self.led.override_background_color(Gtk.StateFlags.NORMAL, color)
    
    def on_button_press(self, widget, data=None):
        print("[DEBUG] Button pressed!")
        print(f"[DEBUG] Checking server connectivity to {self.server_url}")
        # Check server connectivity and toggle LED based on result
        if self.check_server_connectivity():
            print("[DEBUG] Server is reachable, toggling LED")
            self.led_on = not self.led_on
        else:
            print("[DEBUG] Server is NOT reachable, turning LED off")
            # If server is down, turn LED off
            self.led_on = False
        self.update_led()
    
    def on_button_release(self, widget, data=None):
        pass
    
    def get_tool_table_path(self):
        """Get tool table path from INI file."""
        try:
            tool_table = self.config.get('EMCIO', 'TOOL_TABLE', fallback='sim.tbl')
            # If relative path, make it relative to INI directory
            if not os.path.isabs(tool_table):
                ini_dir = os.path.dirname(os.path.join(os.path.dirname(__file__), 'axis.ini'))
                tool_table = os.path.join(ini_dir, tool_table)
            return tool_table
        except:
            return None
    
    def on_backup_button_click(self, widget, data=None):
        """Backup current tool table to server.
        
        Uploads tool table to Smooth using generic tool-presets endpoint.
        Requires parse_tooltable.py to convert LinuxCNC format to Smooth format.
        """
        print("[DEBUG] Backup button clicked")
        
        tool_table_path = self.get_tool_table_path()
        if not tool_table_path or not os.path.exists(tool_table_path):
            print(f"[ERROR] Tool table not found: {tool_table_path}")
            return
        
        try:
            import subprocess
            import json
            
            # Get machine ID from config or use default
            machine_id = self.config.get('SMOOTH', 'MACHINE_ID', fallback='linuxcnc')
            
            # Parse tool table using parse_tooltable.py
            # Script is at repo root: smooth-linuxcnc/parse_tooltable.py
            script_dir = os.path.dirname(__file__)
            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(script_dir))))
            parse_script = os.path.join(repo_root, 'parse_tooltable.py')
            
            result = subprocess.run(
                ['python3', parse_script, tool_table_path, machine_id],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print(f"[ERROR] Failed to parse tool table: {result.stderr}")
                return
            
            # Upload to Smooth
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json'
            } if self.token else {'Content-Type': 'application/json'}
            
            response = requests.post(
                f'{self.server_url}/api/v1/tool-presets',
                headers=headers,
                data=result.stdout,
                timeout=10
            )
            
            if response.status_code in [200, 201]:
                print(f"[SUCCESS] Tool table backed up to server")
            else:
                print(f"[ERROR] Backup failed: HTTP {response.status_code} - {response.text}")
                
        except Exception as e:
            print(f"[ERROR] Backup failed: {str(e)}")
    
    def on_pull_button_click(self, widget, data=None):
        """Pull tool table from server.
        
        Downloads tool presets from Smooth and converts to LinuxCNC format.
        Requires export_tooltable.py to convert Smooth format to LinuxCNC format.
        """
        print("[DEBUG] Pull button clicked")
        
        tool_table_path = self.get_tool_table_path()
        if not tool_table_path:
            print(f"[ERROR] Tool table path not configured")
            return
        
        try:
            import subprocess
            import shutil
            
            # Get machine ID from config or use default
            machine_id = self.config.get('SMOOTH', 'MACHINE_ID', fallback='linuxcnc')
            
            # Download from Smooth
            headers = {'Authorization': f'Bearer {self.token}'} if self.token else {}
            response = requests.get(
                f'{self.server_url}/api/v1/tool-presets?machine_id={machine_id}',
                headers=headers,
                timeout=10
            )
            
            if response.status_code != 200:
                print(f"[ERROR] Pull failed: HTTP {response.status_code} - {response.text}")
                return
            
            # Convert to LinuxCNC format using export_tooltable.py
            # Script is at repo root: smooth-linuxcnc/export_tooltable.py
            script_dir = os.path.dirname(__file__)
            repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(script_dir))))
            export_script = os.path.join(repo_root, 'export_tooltable.py')
            
            result = subprocess.run(
                ['python3', export_script, '-'],
                input=response.text,
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                print(f"[ERROR] Failed to convert tool table: {result.stderr}")
                return
            
            # Create backup before overwriting
            if os.path.exists(tool_table_path):
                backup_path = f"{tool_table_path}.bak"
                shutil.copy2(tool_table_path, backup_path)
                print(f"[INFO] Created backup: {backup_path}")
            
            # Write new tool table
            with open(tool_table_path, 'w') as f:
                f.write(result.stdout)
            print(f"[SUCCESS] Tool table pulled from server")
                
        except Exception as e:
            print(f"[ERROR] Pull failed: {str(e)}")

def get_handlers(halcomp, builder, useropts=None):
    return [HandlerClass(halcomp, builder)]
