import time
import json
from datetime import datetime
from pywinauto import Desktop
from pywinauto.keyboard import send_keys


class BloodFlowTester:
    def __init__(self, app_path=r"C:\Users\vpenn\Documents\OpenWaterApp-0p4\OpenWaterApp.exe"):
        self.app_path = app_path
        self.main_window = None 
        self.test_results = {
            "test_timestamp": datetime.now().isoformat(),
            "app_name": "Open-MOTION BloodFlow", 
            "app_path": app_path, 
            "steps_completed": [],
            "step_details": [],  # Detailed info about each step with timestamps
            "errors": [],
            "status": "Not Started"
        }
    
    def _log_step(self, step_name, success=True, details="", method=""):
        """Helper to log each step with timestamp and details"""
        step_info = {
            "step": step_name,
            "timestamp": datetime.now().isoformat(),
            "success": success,
            "details": details,
            "method": method
        }
        self.test_results["step_details"].append(step_info)
        
        if success:
            summary = f"{step_name}"
            if details:
                summary += f": {details}"
            self.test_results["steps_completed"].append(summary)
        
    def launch_and_connect(self):
        """Step 1: Launch the application (if path provided) and connect to it"""
        print("Step 1: Launching and connecting to application...")
        
        try:
            # Launch the app if path is provided
            if self.app_path:
                print(f"Launching app from: {self.app_path}")
                import subprocess
                subprocess.Popen(self.app_path)
                print("✓ App launched")
                self._log_step("Launch Application", True, f"Launched from {self.app_path}", "subprocess")
                print("Waiting 5 seconds for app to load...")
                time.sleep(5)
            else:
                print("No app path provided. Assuming app is already running...")
                self._log_step("Connect to Running App", True, "App already running", "manual")
            
            # Now connect to the app window
            print("\nPlease click on the Open-MOTION BloodFlow window NOW...")
            print("Waiting 3 seconds for you to click on the app window...")
            time.sleep(3)
            
            # Get the currently focused window
            from pywinauto import Application
            self.main_window = Application(backend="uia").connect(active_only=True).top_window()
            
            window_title = self.main_window.window_text()
            print(f"\n✓ Connected to window: '{window_title}'")
            
            self._log_step("Connect to Window", True, f"Connected to: {window_title}", "pywinauto active_only")
            return True
            
        except Exception as e:
            error_msg = f"Failed to launch/connect to app: {str(e)}"
            self.test_results["errors"].append(error_msg)
            self._log_step("Launch and Connect", False, error_msg)
            print(f"✗ {error_msg}")
            return False
    
    def click_analyze(self):
        """Step 2: Click the Analyze button/tab"""
        print("\nStep 2: Clicking 'Analyze' navigation...")
        try:
            # Try multiple methods to click Analyze
            
            # Method 1: Try to find button by various names
            button_names = ["Analyze", "Analysis", "Data Analysis"]
            for name in button_names:
                try:
                    btn = self.main_window.child_window(title=name, control_type="Button")
                    if btn.exists():
                        btn.click_input()
                        print(f"✓ Clicked '{name}' button")
                        time.sleep(2)
                        self._log_step("Click Analyze", True, f"Clicked '{name}' button", "button_search")
                        return True
                except:
                    continue
            
            # Method 2: Try to find as a tab
            try:
                tab = self.main_window.child_window(title_re=".*Analyz.*", control_type="TabItem")
                if tab.exists():
                    tab.click_input()
                    print("✓ Clicked Analyze tab")
                    time.sleep(2)
                    self._log_step("Click Analyze", True, "Clicked Analyze tab", "tab_search")
                    return True
            except:
                pass
            
            # Method 3: Use coordinates
            print("Using coordinate-based click...")
            rect = self.main_window.rectangle()
            # Click relative to window position
            click_x = rect.left + 40
            click_y = rect.top + 880
            
            import pywinauto.mouse as mouse
            mouse.click(coords=(click_x, click_y))
            
            time.sleep(2)
            self._log_step("Click Analyze", True, f"Clicked at coordinates ({click_x}, {click_y})", "coordinate_click")
            print("✓ Clicked Analyze")
            return True
            
        except Exception as e:
            error_msg = f"Failed to click Analyze: {str(e)}"
            self.test_results["errors"].append(error_msg)
            self._log_step("Click Analyze", False, error_msg)
            print(f"✗ {error_msg}")
            return False
    
    def select_scan_item(self):
        """Step 3: Open scan dropdown and select first item"""
        print("\nStep 3: Selecting scan from dropdown...")
        try:
            # Find any ComboBox in the window
            combos = self.main_window.descendants(control_type="ComboBox")
            
            if combos:
                scan_dropdown = combos[0]  # Get first combobox
                scan_dropdown.click_input()
                time.sleep(1)
                
                # Select first item
                send_keys('{DOWN}{ENTER}')
                time.sleep(2)
                
                self._log_step("Select Scan", True, "Selected first scan from dropdown", "combobox")
                print("✓ Scan item selected")
                return True
            else:
                # Fallback: look for Edit controls
                edits = self.main_window.descendants(control_type="Edit")
                if edits:
                    edits[0].click_input()
                    time.sleep(1)
                    send_keys('{DOWN}{ENTER}')
                    time.sleep(2)
                    
                    self._log_step("Select Scan", True, "Selected scan via edit control", "edit_control")
                    print("✓ Scan item selected")
                    return True
                    
            raise Exception("Could not find scan dropdown")
            
        except Exception as e:
            error_msg = f"Failed to select scan: {str(e)}"
            self.test_results["errors"].append(error_msg)
            self._log_step("Select Scan", False, error_msg)
            print(f"✗ {error_msg}")
            return False
    
    def visualize_bfi_bvi(self):
        """Step 4: Click 'Visualize BFI/BVI' and close window"""
        print("\nStep 4: Clicking 'Visualize BFI/BVI'...")
        try:
            # Find all buttons
            buttons = self.main_window.descendants(control_type="Button")
            
            # Look for BFI/BVI button
            for btn in buttons:
                btn_text = btn.window_text()
                if "BFI" in btn_text or "bfi" in btn_text.lower():
                    btn.click_input()
                    print(f"✓ Clicked '{btn_text}'")
                    time.sleep(10)  # Wait longer for visualization to load
                    
                    # Close visualization window
                    send_keys('%{F4}') # Close visualization
                    time.sleep(2)
                    
                    self._log_step("Visualize BFI/BVI", True, f"Clicked button: {btn_text}", "button_search")
                    print("✓ Closed visualization")
                    return True
                
            print("Using coordinate method for BFI/BVI...")
            rect = self.main_window.rectangle()
            click_x = rect.left + 1038
            click_y = rect.top + 356
            
            import pywinauto.mouse as mouse
            mouse.click(coords=(click_x, click_y))
            time.sleep(3)
            
            send_keys('%{F4}') # Close visualization
            time.sleep(2)
            
            self._log_step("Visualize BFI/BVI", True, f"Clicked at ({click_x}, {click_y})", "coordinate_click")
            print("✓ BFI/BVI completed")
            return True
            
        except Exception as e:
            error_msg = f"Failed to visualize BFI/BVI: {str(e)}"
            self.test_results["errors"].append(error_msg)
            self._log_step("Visualize BFI/BVI", False, error_msg)
            print(f"✗ {error_msg}")
            return False
    
    def visualize_contrast_mean(self):
        """Step 5: Click 'Visualize Contrast/Mean' and close window"""
        print("\nStep 5: Clicking 'Visualize Contrast/Mean'...")
        try:
            # Find all buttons
            buttons = self.main_window.descendants(control_type="Button")
            
            # Look for Contrast/Mean button
            for btn in buttons:
                btn_text = btn.window_text()
                if "Contrast" in btn_text or "contrast" in btn_text.lower() or "Mean" in btn_text:
                    btn.click_input()
                    print(f"✓ Clicked '{btn_text}'")
                    time.sleep(3)
                    
                    # Close visualization window
                    send_keys('%{F4}')
                    time.sleep(1)
                    
                    self._log_step("Visualize Contrast/Mean", True, f"Clicked button: {btn_text}", "button_search")
                    print("✓ Closed visualization")
                    return True
            
            # Fallback
            print("Using coordinate method for Contrast/Mean...")
            rect = self.main_window.rectangle()
            click_x = rect.left + 1038
            click_y = rect.top + 414
            
            import pywinauto.mouse as mouse
            mouse.click(coords=(click_x, click_y))
            time.sleep(3)
            
            send_keys('%{F4}') # Close visualization
            time.sleep(1)
            
            self._log_step("Visualize Contrast/Mean", True, f"Clicked at ({click_x}, {click_y})", "coordinate_click")
            print("✓ Contrast/Mean completed")
            return True
            
        except Exception as e:
            error_msg = f"Failed to visualize Contrast/Mean: {str(e)}"
            self.test_results["errors"].append(error_msg)
            self._log_step("Visualize Contrast/Mean", False, error_msg)
            print(f"✗ {error_msg}")
            return False
    
    def write_report(self, filename="test_report.json"):
        """Step 6: Write comprehensive JSON report"""
        print(f"\nStep 6: Writing test report to {filename}...")
        try:
            # Update status
            if len(self.test_results["errors"]) == 0:
                self.test_results["status"] = "Passed"
            else:
                self.test_results["status"] = "Failed"
            
            self.test_results["test_completion_time"] = datetime.now().isoformat()
            
            # Add summary statistics
            self.test_results["summary"] = {
                "total_steps": len(self.test_results["steps_completed"]),
                "total_errors": len(self.test_results["errors"]),
                "success_rate": f"{(len(self.test_results['steps_completed']) / 6) * 100:.1f}%",
            }
            
            # Write to file with pretty formatting
            with open(filename, 'w') as f:
                json.dump(self.test_results, f, indent=4)
            
            print(f"✓ Report written to {filename}")
            
            # Display summary
            print("\n" + "="*60)
            print(" "*20 + "TEST SUMMARY")
            print("="*60)
            print(f"Status:           {self.test_results['status']}")
            print(f"Steps Completed:  {len(self.test_results['steps_completed'])}/6")
            print(f"Errors:           {len(self.test_results['errors'])}")
            print(f"Success Rate:     {self.test_results['summary']['success_rate']}")
            print(f"Started:          {self.test_results['test_timestamp']}")
            print(f"Completed:        {self.test_results['test_completion_time']}")
            print("="*60)
            
            if self.test_results['steps_completed']:
                print("\n✓ Steps Completed:")
                for i, step in enumerate(self.test_results['steps_completed'], 1):
                    print(f"  {i}. {step}")
            
            if self.test_results['errors']:
                print("\n✗ Errors Encountered:")
                for i, error in enumerate(self.test_results['errors'], 1):
                    print(f"  {i}. {error}")
            
            print("\n" + "="*60)
            print(f"Full report saved to: {filename}")
            print("="*60)
            
            self._log_step("Write Report", True, f"Report saved to {filename}", "json_export")
            return True
        except Exception as e:
            error_msg = f"Failed to write report: {str(e)}"
            self.test_results["errors"].append(error_msg)
            self._log_step("Write Report", False, error_msg)
            print(f"✗ {error_msg}")
            return False
    
    def run_full_test(self):
        """Run the complete test workflow"""
        print("="*60)
        print(" "*15 + "Open-MOTION BloodFlow Test Script")
        print("="*60)
        
        if not self.app_path:
            print("\nMake sure the app is already running!")
        
        # Execute all steps in sequence
        if not self.launch_and_connect():
            return False
        
        if not self.click_analyze():
            return False
        
        if not self.select_scan_item():
            return False
        
        if not self.visualize_bfi_bvi():
            return False
        
        if not self.visualize_contrast_mean():
            return False
        
        self.write_report()
        
        print("\n✓ Test completed!")
        return True


def main():
    """Main entry point"""
    tester = BloodFlowTester(app_path=r"C:\Users\vpenn\Documents\OpenWaterApp-0p4\OpenWaterApp.exe")
    
    tester.run_full_test()


if __name__ == "__main__":
    main()