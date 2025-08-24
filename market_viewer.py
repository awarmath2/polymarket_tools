# Polymarket Interactive Grid Viewer
import tkinter as tk
from tkinter import ttk, messagebox
import requests
import json
import os

class PolymarketViewer:
    def __init__(self, root):
        self.root = root
        self.root.title("Polymarket Market Viewer")
        self.root.geometry("1200x800")
        
        # Configure style
        style = ttk.Style()
        style.theme_use('clam')
        
        # History storage
        self.history_file = "./__cache/slug_history.json"
        self.slug_history = self.load_history()
        
        self.setup_ui()
        
    def load_history(self):
        """Load slug history from file"""
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return []
    
    def save_history(self):
        """Save slug history to file"""
        try:
            with open(self.history_file, 'w') as f:
                json.dump(self.slug_history, f)
        except Exception:
            pass
    
    def add_to_history(self, slug):
        """Add slug to history (avoid duplicates, keep recent items)"""
        if slug in self.slug_history:
            self.slug_history.remove(slug)
        self.slug_history.insert(0, slug)
        # Keep only last 20 items
        self.slug_history = self.slug_history[:20]
        self.save_history()
        # Update combobox values
        self.slug_combobox['values'] = self.slug_history
        
    def setup_ui(self):
        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky="nsew")
        
        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(3, weight=1)
        
        # Input section with history dropdown
        ttk.Label(main_frame, text="Enter/Select Polymarket Slug:").grid(row=0, column=0, sticky="w", pady=(0, 5))
        
        # Create combobox for slug entry with history
        self.slug_combobox = ttk.Combobox(main_frame, width=57, values=self.slug_history)
        self.slug_combobox.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=(0, 5))
        self.slug_combobox.set("will-either-cuomo-or-mamdani-announce-they-are-running-for-mayor-on-non-democrat-slate")
        
        # Fetch button
        self.fetch_button = ttk.Button(main_frame, text="Fetch Market Data", command=self.fetch_data)
        self.fetch_button.grid(row=0, column=2, padx=(10, 0), pady=(0, 5))
        
        # Options frame
        options_frame = ttk.Frame(main_frame)
        options_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))
        
        # Show IDs checkbox
        self.show_ids_var = tk.BooleanVar(value=False)
        self.show_ids_check = ttk.Checkbutton(
            options_frame, 
            text="Show Condition/Question IDs", 
            variable=self.show_ids_var,
            command=self.refresh_display
        )
        self.show_ids_check.pack(side="left", padx=(0, 20))
        
        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready to fetch data")
        self.status_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 10))
        
        # Market info frame - expanded with additional information
        info_frame = ttk.LabelFrame(main_frame, text="Market Information", padding="5")
        info_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        info_frame.columnconfigure(1, weight=1)
        info_frame.columnconfigure(3, weight=1)
        
        # Market info labels - organized in 2 columns
        # Row 0
        self.market_title_label = ttk.Label(info_frame, text="Title: ")
        self.market_title_label.grid(row=0, column=0, columnspan=4, sticky="w")
        
        # Row 1
        self.market_status_label = ttk.Label(info_frame, text="Status: ")
        self.market_status_label.grid(row=1, column=0, sticky="w")
        
        self.market_liquidity_label = ttk.Label(info_frame, text="Liquidity: ")
        self.market_liquidity_label.grid(row=1, column=2, sticky="w", padx=(20, 0))
        
        # Row 2
        self.market_volume_label = ttk.Label(info_frame, text="Volume: ")
        self.market_volume_label.grid(row=2, column=0, sticky="w")
        
        self.market_rewards_label = ttk.Label(info_frame, text="Rewards: ")
        self.market_rewards_label.grid(row=2, column=2, sticky="w", padx=(20, 0))
        
        # Create Treeview for hierarchical display
        tree_frame = ttk.Frame(main_frame)
        tree_frame.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(0, 10))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        
        # Create Treeview with columns for hierarchical display
        # Always include Token ID, optionally include Condition/Question IDs
        if self.show_ids_var.get():
            columns = ("Condition ID", "Question ID", "Outcome", "Price", "Token ID")
        else:
            columns = ("Outcome", "Price", "Token ID")
        
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", height=15)
        
        # Configure columns
        self.tree.heading("#0", text="Market")  # Tree column
        if self.show_ids_var.get():
            self.tree.heading("Condition ID", text="Condition ID")
            self.tree.heading("Question ID", text="Question ID")
        self.tree.heading("Outcome", text="Outcome")
        self.tree.heading("Price", text="Price")
        self.tree.heading("Token ID", text="Token ID")
        
        # Set column widths - expand Market column
        self.tree.column("#0", width=500, minwidth=300)  # Expanded Market column
        if self.show_ids_var.get():
            self.tree.column("Condition ID", width=150, minwidth=100)
            self.tree.column("Question ID", width=150, minwidth=100)
        self.tree.column("Outcome", width=100, minwidth=80)
        self.tree.column("Price", width=80, minwidth=60)
        self.tree.column("Token ID", width=200, minwidth=150)
        
        # Add scrollbars
        tree_scroll_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_scroll_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)
        
        # Grid the tree and scrollbars
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        
        # Bind double-click event for copying (keeping this functionality even though buttons are removed)
        self.tree.bind("<Double-1>", self.copy_selected_cell)
        
        # Store current data for refresh
        self.current_data = None
        
    def fetch_data(self):
        slug = self.slug_combobox.get().strip()
        if not slug:
            messagebox.showerror("Error", "Please enter a slug")
            return
            
        # Add to history
        self.add_to_history(slug)
            
        self.status_label.config(text="Fetching data...")
        self.fetch_button.config(state='disabled')
        self.root.update()
        
        try:
            url = "https://gamma-api.polymarket.com/events"
            params = {"slug": slug}
            
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            if not data:
                self.clear_display()
                self.status_label.config(text="No data found")
                self.current_data = None
                return
                
            self.current_data = data
            self.refresh_display()
            self.status_label.config(text=f"Data fetched successfully - {len(data)} market(s) found")
            
        except requests.exceptions.RequestException as e:
            messagebox.showerror("Error", f"Failed to fetch data: {str(e)}")
            self.status_label.config(text="Error fetching data")
        except json.JSONDecodeError as e:
            messagebox.showerror("Error", f"Invalid JSON response: {str(e)}")
            self.status_label.config(text="Error parsing response")
        except Exception as e:
            messagebox.showerror("Error", f"Unexpected error: {str(e)}")
            self.status_label.config(text="Error occurred")
        finally:
            self.fetch_button.config(state='normal')
    
    def clear_display(self):
        """Clear all displays"""
        self.tree.delete(*self.tree.get_children())
        self.market_title_label.config(text="Title: ")
        self.market_status_label.config(text="Status: ")
        self.market_liquidity_label.config(text="Liquidity: ")
        self.market_volume_label.config(text="Volume: ")
        self.market_rewards_label.config(text="Rewards: ")
    
    def get_rewards_info(self, market):
        """Extract rewards information from market data"""
        clob_rewards = market.get('clobRewards', [])
        if clob_rewards and len(clob_rewards) > 0:
            total_rewards = sum(reward.get('rewardsAmount', 0) for reward in clob_rewards)
            daily_rate = clob_rewards[0].get('rewardsDailyRate', 0)
            return f"${total_rewards:,.2f} (${daily_rate}/day)"
        return "No rewards"
    
    def refresh_display(self):
        """Refresh the display with current data and settings"""
        if self.current_data is None:
            return
            
        self.clear_display()
        
        # Update market info
        market_group = self.current_data[0]  # Assuming single market group
        self.market_title_label.config(text=f"Title: {market_group.get('title', 'N/A')}")
        self.market_status_label.config(text=f"Status: {'Active' if market_group.get('active') else 'Inactive'}")
        self.market_liquidity_label.config(text=f"Liquidity: ${market_group.get('liquidity', 0):,.2f}")
        self.market_volume_label.config(text=f"Volume: ${market_group.get('volume', 0):,.2f}")
        
        # Get additional info from first market (if available)
        markets = market_group.get('markets', [])
        if markets:
            first_market = markets[0]
            rewards_info = self.get_rewards_info(first_market)
            self.market_rewards_label.config(text=f"Rewards: {rewards_info}")
        
        # Recreate tree with correct columns
        self.recreate_tree()
        
        # Populate tree with hierarchical structure
        for i, market in enumerate(markets, 1):
            question = market.get('question', 'N/A')
            condition_id = market.get('conditionId', 'N/A')
            question_id = market.get('questionID', 'N/A')
            accepting_orders = market.get('acceptingOrders', True)
            
            # Create parent node for the market question
            if self.show_ids_var.get():
                # With IDs: Condition ID, Question ID, Outcome, Price, Token ID
                parent_values = (condition_id, question_id, "", "", "")
            else:
                # Without IDs: Outcome, Price, Token ID
                parent_values = ("", "", "")
            
            # Set tag based on acceptingOrders status
            parent_tag = 'market_inactive' if not accepting_orders else 'market'
            
            parent_item = self.tree.insert("", "end", text=f"Market {i}: {question[:50]}{'...' if len(question) > 50 else ''}", 
                                         values=parent_values, tags=(parent_tag,))
            
            # Get outcomes
            outcomes = market.get('outcomes', '[]')
            outcome_prices = market.get('outcomePrices', '[]')
            clob_token_ids = market.get('clobTokenIds', '[]')
            
            try:
                outcomes_list = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
                prices_list = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                token_ids_list = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
                
                # Check if we should auto-expand this market
                should_expand = True
                for price in prices_list:
                    try:
                        price_float = float(price)
                        if price_float == 0.0 or price_float == 1.0:
                            should_expand = False
                            break
                    except (ValueError, TypeError):
                        pass
                
                for outcome, price, token_id in zip(outcomes_list, prices_list, token_ids_list):
                    # Map values correctly based on column configuration
                    if self.show_ids_var.get():
                        # With IDs: Condition ID, Question ID, Outcome, Price, Token ID
                        child_values = ("", "", outcome, price, token_id)
                    else:
                        # Without IDs: Outcome, Price, Token ID
                        child_values = (outcome, price, token_id)
                    
                    # Set tag based on acceptingOrders status
                    child_tag = 'outcome_inactive' if not accepting_orders else 'outcome'
                    
                    child_item = self.tree.insert(parent_item, "end", text=f"  {outcome}", 
                                   values=child_values, tags=(child_tag,))
                
                # Auto-expand if conditions are met
                if should_expand:
                    self.tree.item(parent_item, open=True)
                    
            except (json.JSONDecodeError, TypeError, IndexError):
                # Insert error row with correct value mapping
                if self.show_ids_var.get():
                    child_values = ("", "", "Error", "Error", "Error")
                else:
                    child_values = ("Error", "Error", "Error")
                self.tree.insert(parent_item, "end", text="  Error", 
                               values=child_values, tags=('error',))
        
        # Configure tags for styling - including inactive states
        self.tree.tag_configure('market', background='#f0f0f0', font=('TkDefaultFont', 9, 'bold'))
        self.tree.tag_configure('market_inactive', background='#d0d0d0', foreground='#808080', font=('TkDefaultFont', 9, 'bold'))
        self.tree.tag_configure('outcome', background='#ffffff', font=('TkDefaultFont', 8))
        self.tree.tag_configure('outcome_inactive', background='#f0f0f0', foreground='#808080', font=('TkDefaultFont', 8))
        self.tree.tag_configure('error', background='#ffebee', font=('TkDefaultFont', 8))
    
    def recreate_tree(self):
        """Recreate the tree with correct columns based on settings"""
        # Clear existing tree
        self.tree.delete(*self.tree.get_children())
        
        # Define columns based on settings - always include Token ID
        if self.show_ids_var.get():
            columns = ("Condition ID", "Question ID", "Outcome", "Price", "Token ID")
        else:
            columns = ("Outcome", "Price", "Token ID")
        
        # Reconfigure tree
        self.tree.configure(columns=columns)
        
        # Reconfigure headings and columns
        self.tree.heading("#0", text="Market")  # Tree column
        if self.show_ids_var.get():
            self.tree.heading("Condition ID", text="Condition ID")
            self.tree.heading("Question ID", text="Question ID")
        self.tree.heading("Outcome", text="Outcome")
        self.tree.heading("Price", text="Price")
        self.tree.heading("Token ID", text="Token ID")
        
        # Set column widths - expand Market column
        self.tree.column("#0", width=500, minwidth=300)  # Expanded Market column
        if self.show_ids_var.get():
            self.tree.column("Condition ID", width=150, minwidth=100)
            self.tree.column("Question ID", width=150, minwidth=100)
        self.tree.column("Outcome", width=100, minwidth=80)
        self.tree.column("Price", width=80, minwidth=60)
        self.tree.column("Token ID", width=200, minwidth=150)
    
    def copy_selected_cell(self, event=None):
        """Copy the selected cell content to clipboard"""
        try:
            selection = self.tree.selection()
            if not selection:
                return
                
            item = selection[0]
            
            # Handle tree column (#0) or data columns
            if event and self.tree.identify_region(event.x, event.y) == "tree":
                # Tree column - copy the text
                cell_value = self.tree.item(item, "text")
            else:
                # Data columns
                column = self.tree.identify_column(event.x) if event else "#1"
                column_id = int(column[1]) - 1  # Convert #1, #2, etc. to 0, 1, etc.
                
                values = self.tree.item(item, "values")
                if values and column_id < len(values):
                    cell_value = values[column_id]
                else:
                    return
            
            self.root.clipboard_clear()
            self.root.clipboard_append(str(cell_value))
            messagebox.showinfo("Success", f"Copied: {cell_value}")
                
        except Exception as e:
            messagebox.showerror("Error", f"Failed to copy cell: {str(e)}")

def main():
    root = tk.Tk()
    app = PolymarketViewer(root)
    root.mainloop()

if __name__ == "__main__":
    main()
