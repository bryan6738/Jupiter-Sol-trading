from threading import Thread
from datetime import datetime
import time
import os
import json
import traceback
from solders.signature import Signature
from solana.exceptions import SolanaRpcException

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

from bridge import Bridge
from config import BUY_QUANTITY_IN_SOL, SLIPPAGE_BPS_BUY, SLIPPAGE_BPS_SELL, PRIORITIZATION_FEE_BUY, PRIORITIZATION_FEE_SELL

import logging
from logging.handlers import TimedRotatingFileHandler

# Create a logger
logger = logging.getLogger("SolanaCopyTraderLogger")
logger.setLevel(logging.INFO)

# Create a TimedRotatingFileHandler with UTC time
handler = TimedRotatingFileHandler(
    os.path.join('logs', 'daily_log.log'), when="midnight", interval=1, backupCount=30, utc=True
)
handler.suffix = "%Y-%m-%d"  # Adding the date to the log filename
handler.setLevel(logging.INFO)

# Create a formatter and set it for the handler
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

# Add the handler to the logger
logger.addHandler(handler)

class Strategy:
    def __init__(self):
        self.bridge = Bridge(self)
        
        self.buy_amt_in_sol = BUY_QUANTITY_IN_SOL
        
        self.saved_transactions = []
        self.tx_attempts_cnt = {}
        
        self.sol_mint_address = 'So11111111111111111111111111111111111111112'
    
    def start(self):
        # Start streaming and tracking wallets
        Thread(target=self.bridge.start_ws).start()
        
        self.bridge.get_wallet_balances()
        
        try:
            while True:
                time.sleep(1)  # Keep the main thread alive
        except KeyboardInterrupt:
            print("Exiting...")
    
    def determine_transaction_details(self, old_balance, new_balance):
        # (transaction_type, side, input_mint, output_mint, input_amount, output_amount)
        # transction_type - swap, transferIn, transferOut, unknown
        # side - buy, sell
        # input_mint - {address, start_balance, end_balance}
        # output_mint - {address, start_balance, end_balance}
        
        sol_mint_address = 'So11111111111111111111111111111111111111112'
        # usdc_mint_address = 'EPjFWdd5AufqSSQBp1PeBqDdwywQ8kD3QNeGExkqiZmQ'
                
        if len(old_balance) < len(new_balance):
            # A new token has been added
            new_tokens = [t for t in new_balance.keys() if t not in old_balance.keys()]
            
            if len(new_tokens) == 1:
                buy_token = new_tokens[0]
                buy_amount = new_balance[buy_token]
                
                common_tokens = [t for t in new_balance.keys() if t in old_balance.keys()]
                sell_tokens = [t for t in common_tokens if old_balance[t] > new_balance[t]]
                
                if len(sell_tokens) == 0:
                    # transferIn
                    transaction_type = 'transferIn'
                    side = ''
                    input_mint = ''
                    output_mint = buy_token
                    input_amount = None
                    output_amount = buy_amount
                    comment = ''
                    
                    # self.print_wallet_balance_comparison(old_balance, new_balance)
                
                else:
                    
                    comment = ''
                    if len(sell_tokens) == 1:
                        transaction_type = 'swap'
                        sell_token = sell_tokens[0]
                        sell_amount = old_balance[sell_token] - new_balance[sell_token]
                    elif len(sell_tokens) == 2:
                        transaction_type = 'unknown'
                        sell_token = 'unknown'
                        sell_amount = None
                        comment = 'Two tokens decreased in transaction'
                        # self.print_wallet_balance_comparison(old_balance, new_balance)
                    else:
                        transaction_type = 'unknown'
                        sell_token = 'unknown'
                        sell_amount = None
                        comment = 'More than two tokens decreased in transaction'
                        # self.print_wallet_balance_comparison(old_balance, new_balance)
                        
                    transaction_type = transaction_type
                    side = 'buy' if sell_token == sol_mint_address else 'sell'
                    input_mint = sell_token
                    output_mint = buy_token
                    input_amount = sell_amount
                    output_amount = buy_amount
                    comment = comment
                    
            else:
                transaction_type = 'unknown'
                side = ''
                input_mint = ''
                output_mint = ''
                input_amount = None
                output_amount = None
                comment = 'More than one new token added in transaction'
                # self.print_wallet_balance_comparison(old_balance, new_balance)
                
        elif len(old_balance) > len(new_balance):
            # A token has been removed
            removed_tokens = [t for t in old_balance.keys() if t not in new_balance.keys()]
            
            if len(removed_tokens) == 1:
                sell_token = removed_tokens[0]
                sell_amount = old_balance[sell_token]
                
                common_tokens = [t for t in old_balance.keys() if t in new_balance.keys()]
                buy_tokens = [t for t in common_tokens if new_balance[t] > old_balance[t]]
                
                if len(buy_tokens) == 0:
                    # transferOut
                    transaction_type = 'transferOut'
                    side = ''
                    input_mint = sell_token
                    output_mint = ''
                    input_amount = sell_amount
                    output_amount = None
                    comment = ''
                    # self.print_wallet_balance_comparison(old_balance, new_balance)
                    
                else:
                    
                    comment = ''
                    if len(buy_tokens) == 1:
                        transaction_type = 'swap'
                        buy_token = buy_tokens[0]
                        buy_amount = new_balance[buy_token] - new_balance[buy_token]
                    else:
                        transaction_type = 'unknown'
                        buy_token = 'unknown'
                        buy_amount = None
                        comment = 'More than one token increased in transaction'
                        # self.print_wallet_balance_comparison(old_balance, new_balance)
                        
                    transaction_type = transaction_type
                    side = 'sell'
                    input_mint = sell_token
                    output_mint = buy_token
                    input_amount = sell_amount
                    output_amount = buy_amount
                    comment = comment
                    
            else:
                transaction_type = 'unknown'
                side = ''
                input_mint = ''
                output_mint = ''
                input_amount = None
                output_amount = None
                comment = 'More than one token removed in transaction'
                # self.print_wallet_balance_comparison(old_balance, new_balance)
        
        else:
            
            if old_balance.keys() == new_balance.keys():
                # Change in qty
                increased_tokens = [t for t in new_balance.keys() if new_balance[t] > old_balance[t]]
                decreased_tokens = [t for t in new_balance.keys() if new_balance[t] < old_balance[t]]
            else:
                increased_tokens = [t for t in new_balance.keys() if t not in old_balance.keys()]
                decreased_tokens = [t for t in old_balance.keys() if t not in new_balance.keys()]
                        
            if len(increased_tokens) == 1:
                if len(decreased_tokens) == 0:
                    # transferIn
                    transaction_type = 'transferIn'
                    side = ''
                    input_mint = ''
                    output_mint = increased_tokens[0]
                    input_amount = None
                    output_amount = new_balance[output_mint] - old_balance[output_mint] if output_mint in old_balance else new_balance[output_mint]
                    comment = ''
                    # self.print_wallet_balance_comparison(old_balance, new_balance)
                
                elif len(decreased_tokens) == 1:
                    # swap with sol
                    transaction_type = 'swap'
                    side = 'buy' if decreased_tokens[0] == sol_mint_address else 'sell'
                    input_mint = decreased_tokens[0]
                    output_mint = increased_tokens[0]
                    input_amount = old_balance[decreased_tokens[0]] - new_balance[decreased_tokens[0]] if decreased_tokens[0] in new_balance else old_balance[decreased_tokens[0]]
                    output_amount = new_balance[increased_tokens[0]] - old_balance[increased_tokens[0]] if increased_tokens[0] in old_balance else new_balance[increased_tokens[0]]
                    comment = ''
                
                elif len(decreased_tokens) == 2:
                    # unknown
                    transaction_type = 'unknown'
                    side = ''
                    input_mint = ''
                    output_mint = ''
                    input_amount = None
                    output_amount = None
                    comment = 'Two tokens decreased in transaction'
                    # self.print_wallet_balance_comparison(old_balance, new_balance)
                    
                else:
                    # unknown
                    transaction_type = 'unknown'
                    side = ''
                    input_mint = ''
                    output_mint = ''
                    input_amount = None
                    output_amount = None
                    comment = 'More than two tokens decreased in transaction'
                    # self.print_wallet_balance_comparison(old_balance, new_balance)
            
            elif len(decreased_tokens) == 1:
                if len(increased_tokens) == 0:
                    # transferOut
                    transaction_type = 'transferOut'
                    side = ''
                    input_mint = decreased_tokens[0]
                    output_mint = ''
                    input_amount = old_balance[input_mint] - new_balance[input_mint] if input_mint in new_balance else old_balance[input_mint]
                    output_amount = None
                    comment = ''
                    # self.print_wallet_balance_comparison(old_balance, new_balance)
                
                else:
                    # unknown
                    transaction_type = 'unknown'
                    side = ''
                    input_mint = ''
                    output_mint = ''
                    input_amount = None
                    output_amount = None
                    comment = 'More than one token increased in transaction'
                    # self.print_wallet_balance_comparison(old_balance, new_balance)
            
            else:
                # unknown
                transaction_type = 'unknown'
                side = ''
                input_mint = ''
                output_mint = ''
                input_amount = None
                output_amount = None
                comment = 'More than one token increased and decreased in transaction'
                # self.print_wallet_balance_comparison(old_balance, new_balance)
                
        transaction_details = {'transaction_type': transaction_type, 
                               'side': side, 
                               'input_mint': input_mint, 
                               'output_mint': output_mint, 
                               'input_amount': input_amount, 
                               'output_amount': output_amount, 
                               'comment': comment}
        
        return transaction_details
    
    def print_wallet_balance_comparison(self, old_balance, new_balance):
        print('Old balance:')
        print(old_balance)
        print('New balance:')
        print(new_balance)
    
    def append_new_transaction(self, wallet_pubkey_str, txid_tracked, transaction_ts, transaction_finalization_ts, 
                               transaction_details, old_balance, new_balance, action_details):
        # transaction_ts, transaction_type, side, 
        # input_mint_addr, input_mint_start, input_mint_end, input_mint_amount, 
        # output_mint_addr, output_mint_start, output_mint_end, output_mint_amount,
        # action_taken, action_ts, txid, tx_status, tx_status_ts
        
        try:
        
            new_tx = {'transaction_ts': transaction_ts.strftime('%Y-%m-%d %H:%M:%S'),
                      'transacting_wallet': wallet_pubkey_str,
                      'transaction_id': txid_tracked,
                      'transaction_type': transaction_details['transaction_type'], 
                      'side': transaction_details['side'], 
                      'input_mint_addr': transaction_details['input_mint'], 
                      'input_mint_start': old_balance[transaction_details['input_mint']] if transaction_details['input_mint'] in old_balance else None, 
                      'input_mint_end': new_balance[transaction_details['input_mint']] if transaction_details['input_mint'] in new_balance else None,
                      'input_mint_amount': transaction_details['input_amount'], 
                      'output_mint_addr': transaction_details['output_mint'], 
                      'output_mint_start': old_balance[transaction_details['output_mint']] if transaction_details['output_mint'] in old_balance else None, 
                      'output_mint_end': new_balance[transaction_details['output_mint']] if transaction_details['output_mint'] in new_balance else None,
                      'output_mint_amount': transaction_details['output_amount'], 
                      'transaction_finalization_ts': transaction_finalization_ts.strftime('%Y-%m-%d %H:%M:%S'),
                      'action_ts': action_details['action_ts'].strftime('%Y-%m-%d %H:%M:%S') if action_details['action_ts'] != None else None,
                      'action_taken': action_details['action_taken'], 
                      'txid': action_details['txid'], 
                      'tx_status': action_details['tx_status'], 
                      'tx_status_ts': action_details['tx_status_ts'].strftime('%Y-%m-%d %H:%M:%S') if action_details['tx_status_ts'] != None else None
                      }
            
            if 'transactions.json' in os.listdir('trackers'):
                while True:
                    try:
                        with open(os.path.join('trackers', 'transactions.json'), 'r') as f:
                            saved_transactions = json.load(f)
                        break
                    except json.decoder.JSONDecodeError:
                        time.sleep(0.1)
                        continue
            else:
                saved_transactions = []
                
            saved_transactions.append(new_tx)
            self.saved_transactions = saved_transactions
            
            with open(os.path.join('trackers', 'transactions.json'), 'w') as f:
                json.dump(self.saved_transactions, f, indent=4)
                
        except Exception as e:
            self.handle_exception('append_new_transaction', e)
            
    def update_existing_transaction(self, wallet_pubkey_str, txid_tracked, action_details):
        
        try:
            
            idx = [idx for idx,v in enumerate(self.saved_transactions) if v['transacting_wallet'] == wallet_pubkey_str and v['transaction_id'] == txid_tracked][0]
            self.saved_transactions[idx]['action_ts'] = action_details['action_ts'].strftime('%Y-%m-%d %H:%M:%S') if action_details['action_ts'] != None else None
            self.saved_transactions[idx]['action_taken'] = action_details['action_taken']
            self.saved_transactions[idx]['txid'] = action_details['txid']
            self.saved_transactions[idx]['tx_status'] = action_details['tx_status']
            self.saved_transactions[idx]['tx_status_ts'] = action_details['tx_status_ts'].strftime('%Y-%m-%d %H:%M:%S') if action_details['tx_status_ts'] != None else None
            
            with open(os.path.join('trackers', 'transactions.json'), 'w') as f:
                json.dump(self.saved_transactions, f, indent=4)
                
        except Exception as e:
            self.handle_exception('update_existing_transaction', e)
    
    def run_strategy(self, wallet_pubkey_str, transaction_ts, tx, txid_tracked):
        
        try:
            
            self.bridge.get_wallet_balance(self.bridge.home_wallet, self.bridge.home_wallet_pubkey)
            balance = self.bridge.balances[self.bridge.home_wallet]
            sol_balance = balance.get(self.sol_mint_address, 0)  # Get the current SOL balance
            
            # Add the check for minimum SOL balance
            if sol_balance < 0.02:
                print("Wallet SOL balance too low.")
                return  # Skip transaction if SOL balance is too low       
            
            idx = tx['result']['transaction']['message']['accountKeys'].index(wallet_pubkey_str)
            preSOLBalance = {self.sol_mint_address: tx['result']['meta']['preBalances'][idx] / 10**9}
            postSOLBalance = {self.sol_mint_address: (tx['result']['meta']['postBalances'][idx] + tx['result']['meta']['fee']) / 10**9}
            
            preTokenBalances = [bal for bal in tx['result']['meta']['preTokenBalances'] if bal['owner'] == wallet_pubkey_str]
            preTokenBalances = {x['mint']:x['uiTokenAmount']['uiAmount'] if x['uiTokenAmount']['uiAmount'] != None else 0 for x in preTokenBalances}
            
            postTokenBalances = [bal for bal in tx['result']['meta']['postTokenBalances'] if bal['owner'] == wallet_pubkey_str]
            postTokenBalances = {x['mint']:x['uiTokenAmount']['uiAmount'] if x['uiTokenAmount']['uiAmount'] != None else 0 for x in postTokenBalances}
            
            old_balance = {**preSOLBalance, **preTokenBalances}
            new_balance = {**postSOLBalance, **postTokenBalances}
            
            old_balance = {k:v for k,v in old_balance.items() if v > 0}
            new_balance = {k:v for k,v in new_balance.items() if v > 0}
            
            transaction_details = self.determine_transaction_details(old_balance, new_balance)
            
            transaction_finalization_ts = transaction_ts
            self.log_msg(f'Transaction details - {str(transaction_details)}', 'INFO')
            
            if transaction_details['transaction_type'] != 'swap':
                msg = f'{wallet_pubkey_str} - {txid_tracked} - {transaction_details["transaction_type"]} transaction. Skipping.'
                self.print_msg(msg)
            
            elif transaction_details['transaction_type'] == 'swap':
                input_mint_addr = transaction_details['input_mint']
                output_mint_addr = transaction_details['output_mint']
                
                if txid_tracked not in self.tx_attempts_cnt:
                    self.tx_attempts_cnt[txid_tracked] = 1
                else:
                    self.tx_attempts_cnt[txid_tracked] += 1
                
                if transaction_details['side'] == 'buy':
                    
                    if input_mint_addr == 'So11111111111111111111111111111111111111112':
                        amount = self.buy_amt_in_sol
                        action_taken = f'Swap {amount} SOL for outputMint'
                    else:
                        price = self.bridge.get_jupiter_price_vs_sol(input_mint_addr, self.buy_amt_in_sol)
                        amount = self.buy_amt_in_sol / price
                        action_taken = f'Swap {amount} inputMint for outputMint'
                    
                    self.bridge.get_wallet_balance(self.bridge.home_wallet, self.bridge.home_wallet_pubkey)
                    balance = self.bridge.balances[self.bridge.home_wallet]
                    balance_inputMint = balance[input_mint_addr] if input_mint_addr in balance else 0
                    
                    if balance_inputMint > amount:
                    
                        slippage_bps = SLIPPAGE_BPS_BUY
                        prioritization_fee = int(PRIORITIZATION_FEE_BUY * 10**9)
                        txid = self.bridge.swap_via_jupiter(input_mint_addr, output_mint_addr, amount, slippage_bps, prioritization_fee)
                        self.bridge.tx_details_mapping[txid] = {'address': wallet_pubkey_str, 
                                                                'txid_tracked': txid_tracked, 
                                                                'tx_tracked': tx, 
                                                                'transaction_ts': transaction_ts}
                        
                        action_details = {'action_taken': action_taken, 
                                          'action_ts': datetime.utcfromtimestamp(time.time()), 
                                          'txid': txid if txid != None else 'Tx failed. See logs.', 
                                          'tx_status': 'Waiting' if txid != None else None, 
                                          'tx_status_ts': None}
                        
                        if self.tx_attempts_cnt[txid_tracked] == 1:
                            self.append_new_transaction(wallet_pubkey_str, txid_tracked, transaction_ts, transaction_finalization_ts, transaction_details, old_balance, new_balance, action_details)
                        else:
                            self.update_existing_transaction(wallet_pubkey_str, txid_tracked, action_details)
                        
                        retries = 0
                        while retries < 5:
                            tx_success = self.bridge.poll_tx_for_status(txid)
                            if tx_success:
                                break
                            else:
                                self.print_msg(f'{txid} - Copy transaction sent but not processed. Retrying.')
                                old_txid = txid
                                txid = self.bridge.swap_via_jupiter(input_mint_addr, output_mint_addr, amount, slippage_bps, prioritization_fee)
                                self.bridge.tx_details_mapping[txid] = {'address': wallet_pubkey_str, 
                                                                        'txid_tracked': txid_tracked, 
                                                                        'tx_tracked': tx, 
                                                                        'transaction_ts': transaction_ts}
                                self.update_txid(old_txid, txid)
                                retries += 1
                                
                        if retries == 5:
                            self.print_msg(f'{txid} - Transaction failed')
                            for t in self.saved_transactions:
                                if t['txid'] == txid:
                                    t['tx_status'] = 'unprocessed'
                                    t['tx_status_ts'] = datetime.utcfromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
                                    break
                    
                    else:
                        action_details = {'action_taken': 'Insufficient SOL balance. No action taken', 
                                          'action_ts': datetime.utcfromtimestamp(time.time()), 
                                          'txid': None, 
                                          'tx_status': None, 
                                          'tx_status_ts': None}
                        self.print_msg(f'{txid_tracked} - Insufficient SOL balance. No action taken')
                        
                        if self.tx_attempts_cnt[txid_tracked] == 1:
                            self.append_new_transaction(wallet_pubkey_str, txid_tracked, transaction_ts, transaction_finalization_ts, transaction_details, old_balance, new_balance, action_details)
                        else:
                            self.update_existing_transaction(wallet_pubkey_str, txid_tracked, action_details)
                        
                elif transaction_details['side'] == 'sell':
                    
                    perc_change = transaction_details['input_amount'] / old_balance[input_mint_addr]
                    
                    self.bridge.get_wallet_balance(self.bridge.home_wallet, self.bridge.home_wallet_pubkey)
                    balance = self.bridge.balances[self.bridge.home_wallet]
                    balance_inputMint = balance[input_mint_addr] if input_mint_addr in balance else 0
                    if balance_inputMint > 0:
                        amount = balance_inputMint * perc_change
                        slippage_bps = SLIPPAGE_BPS_SELL
                        prioritization_fee = int(PRIORITIZATION_FEE_SELL * 10**9)
                        txid = self.bridge.swap_via_jupiter(input_mint_addr, output_mint_addr, amount, slippage_bps, prioritization_fee)
                        self.bridge.tx_details_mapping[txid] = {'address': wallet_pubkey_str, 
                                                                'txid_tracked': txid_tracked, 
                                                                'tx_tracked': tx, 
                                                                'transaction_ts': transaction_ts}
                        
                        action_details = {'action_taken': f'Swap {amount} inputMint for outputMint', 
                                          'action_ts': datetime.utcfromtimestamp(time.time()), 
                                          'txid': txid if txid != None else 'Tx failed. See logs.', 
                                          'tx_status': 'Waiting' if txid != None else None, 
                                          'tx_status_ts': None}
                        
                        if self.tx_attempts_cnt[txid_tracked] == 1:
                            self.append_new_transaction(wallet_pubkey_str, txid_tracked, transaction_ts, transaction_finalization_ts, transaction_details, old_balance, new_balance, action_details)
                        else:
                            self.update_existing_transaction(wallet_pubkey_str, txid_tracked, action_details)
                        
                        retries = 0
                        while retries < 5:
                            tx_success = self.bridge.poll_tx_for_status(txid)
                            if tx_success:
                                break
                            else:
                                self.print_msg(f'{txid} - Copy transaction sent but not processed. Retrying.')
                                old_txid = txid
                                txid = self.bridge.swap_via_jupiter(input_mint_addr, output_mint_addr, amount, slippage_bps, prioritization_fee)
                                self.bridge.tx_details_mapping[txid] = {'address': wallet_pubkey_str, 
                                                                        'txid_tracked': txid_tracked, 
                                                                        'tx_tracked': tx, 
                                                                        'transaction_ts': transaction_ts}
                                self.update_txid(old_txid, txid)
                                retries += 1
                                
                        if retries == 5:
                            self.print_msg(f'{txid} - Transaction failed')
                            for t in self.saved_transactions:
                                if t['txid'] == txid:
                                    t['tx_status'] = 'unprocessed'
                                    t['tx_status_ts'] = datetime.utcfromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
                                    break
                        
                    else:
                        action_details = {'action_taken': 'Insufficient balance. No action taken', 
                                          'action_ts': datetime.utcfromtimestamp(time.time()), 
                                          'txid': None, 
                                          'tx_status': None, 
                                          'tx_status_ts': None}
                        self.print_msg(f'{txid_tracked} - Insufficient balance. No action taken')
                        
                        if self.tx_attempts_cnt[txid_tracked] == 1:
                            self.append_new_transaction(wallet_pubkey_str, txid_tracked, transaction_ts, transaction_finalization_ts, transaction_details, old_balance, new_balance, action_details)
                        else:
                            self.update_existing_transaction(wallet_pubkey_str, txid_tracked, action_details)
                        
        except Exception as e:
            self.print_msg(f'Error while processing txid {txid_tracked}')
            logger.error(f'Exception in run_strategy: {e}')
            logger.error(traceback.format_exc())
    
    def update_txid(self, old_txid, new_txid):
        for t in self.saved_transactions:
            if t['txid'] == old_txid:
                t['txid'] = new_txid
                break
        with open(os.path.join('trackers', 'transactions.json'), 'w') as f:
            json.dump(self.saved_transactions, f, indent=4)
    
    def process_confirmed_transaction(self, txid, confirmation_ts, confirmation):
        
        confirmed = False
        for t in self.saved_transactions:
            if t['txid'] == txid:
                if isinstance(confirmation, dict) and 'InstructionError' in confirmation and len(confirmation['InstructionError']) > 1 and isinstance(confirmation['InstructionError'][1], dict) and 'Custom' in confirmation['InstructionError'][1]:
                    if confirmation['InstructionError'][1]['Custom'] == 6001:
                        if txid in self.bridge.tx_details_mapping:
                            tx_details = self.bridge.tx_details_mapping[txid]
                            address, transaction_ts, tx, txid_tracked = tx_details['address'], tx_details['transaction_ts'], tx_details['tx_tracked'], tx_details['txid_tracked']
                            if txid_tracked not in self.tx_attempts_cnt or self.tx_attempts_cnt[txid_tracked] < 5:
                                Thread(target=self.run_strategy, args=(address, transaction_ts, tx, txid_tracked,)).start()
                                if txid_tracked not in self.tx_attempts_cnt:
                                    self.tx_attempts_cnt[txid_tracked] = 1
                                else:
                                    self.tx_attempts_cnt[txid_tracked] += 1
                                t['tx_status'] = 'failed due to slippageTolerance. Retrying...'
                                self.print_msg(f'{txid} - Transaction failed due to slippageTolerance. Retrying...')
                                confirmation = 'failed due to slippageTolerance. Retrying...'
                            else:
                                t['tx_status'] = 'failed due to slippageTolerance'
                                confirmation = 'failed due to slippageTolerance'
                        else:
                            t['tx_status'] = 'failed due to slippageTolerance'
                            confirmation = 'failed due to slippageTolerance'
                    else:
                        t['tx_status'] = confirmation
                else:
                    t['tx_status'] = confirmation
                t['tx_status_ts'] = confirmation_ts.strftime('%Y-%m-%d %H:%M:%S')
                if confirmation == 'confirmed':
                    self.print_msg(f'{txid} - Transaction confirmed')
                elif confirmation == 'failed due to slippageTolerance. Retrying...':
                    pass
                elif confirmation == 'failed due to slippageTolerance':
                    self.print_msg(f'{txid} - Transaction failed due to slippageTolerance')
                else:
                    self.print_msg(f'{txid} - Transaction failed. See logs.')
                confirmed = True
                break
        
        if not confirmed:
            self.print_msg(f'{txid} - Transaction failed')
        
        with open(os.path.join('trackers', 'transactions.json'), 'w') as f:
            json.dump(self.saved_transactions, f, indent=4)
            
    def process_missed_transactions(self):
        
        connection_lost_time = self.bridge.ws_connection_lost_time
                
        for address in self.bridge.tracked_wallets:
            
            if address in self.bridge.last_tx_sig:
                signatures = self.bridge.solana_client.get_signatures_for_address(self.bridge.tracked_wallet_pubkeys[address], until=Signature.from_string(self.bridge.last_tx_sig[address]), commitment='confirmed')
                signatures = json.loads(signatures.to_json())
            else:
                signatures = self.bridge.solana_client.get_signatures_for_address(self.bridge.tracked_wallet_pubkeys[address], commitment='confirmed')
                signatures = json.loads(signatures.to_json())
            
            if 'result' in signatures and len(signatures['result']) > 0:
                
                signatures['result'] = [x for x in signatures['result'] if datetime.utcfromtimestamp(x['blockTime']) >= connection_lost_time]
                
                for transaction in signatures['result']:
                    txid_tracked = transaction['signature']
                    transaction_ts = datetime.utcfromtimestamp(transaction['blockTime'])
                    
                    n = 0
                    while n < 10:
                        try:
                            tx = self.bridge.solana_client.get_transaction(Signature.from_string(txid_tracked), commitment='confirmed', max_supported_transaction_version=0)
                            tx = json.loads(tx.to_json())
                            if 'result' in tx and tx['result'] != None and 'transaction' in tx['result'] and 'message' in tx['result']['transaction']:
                                break
                            else:
                                n += 1
                        except SolanaRpcException:
                            n += 1
                            continue
                    
                    if n >= 10:
                        self.print_msg(f'{address} - Unable to retrieve transaction info. Skipping. - {txid_tracked}')
                    
                    else:
                        Thread(target=self.run_strategy, args=(address, transaction_ts, tx, txid_tracked,)).start()
    
    def save_last_txid(self):    
        save_dict = {}
        for address in self.bridge.tracked_wallets:
            save_dict[address] = {'last_txid': self.bridge.last_tx_sig[address] if address in self.bridge.last_tx_sig else '', 
                                  'last_tx_time': self.bridge.last_tx_time[address].strftime('%Y-%m-%d %H:%M:%S') if address in self.bridge.last_tx_time else ''}
        with open(os.path.join('trackers', 'last_tx.json'), 'w') as f:
            json.dump(save_dict, f, indent=4)
    
    def print_msg(self, msg):
        ts = datetime.utcfromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
        print(f'{ts} - {msg}')
    
    def log_msg(self, msg, level):
        if level == 'INFO':
            logger.info(msg)
        elif level == 'ERROR':
            logger.error(msg)
    
    def handle_exception(self, loc, e):
        print(f'Exception in {loc}: {e}')
        logger.error(f'Exception in {loc}: {e}')
        logger.error(traceback.format_exc())
    
if __name__ == '__main__':
    strategy = Strategy()
    strategy.start()