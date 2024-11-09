from solana.rpc.api import Client
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TokenAccountOpts, TxOpts
from solana.rpc.commitment import Processed
from solana.exceptions import SolanaRpcException
from solders.pubkey import Pubkey
from solders.keypair import Keypair
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from solders import message
import base58
import base64
import struct

import asyncio
import websockets
import json
from datetime import datetime
import time
import requests
from threading import Thread

from config import RPC_API_URL, RPC_WSS_URL
from config import HOME_WALLET_PUBKEY, HOME_WALLET_PRIVATE_KEY
from config import TRACKED_WALLET_PUBKEYS

def handle_unhandled_exception(loop, context):
    exception = context.get('exception')
    message = context.get('message', 'No message')
    print(f"{datetime.utcnow()} - Unhandled exception: {message}")
    if exception:
        print(f"Exception details: {exception}")


# Set up the custom exception handler
loop = asyncio.get_event_loop()
loop.set_exception_handler(handle_unhandled_exception)

class Bridge:
    def __init__(self, _strategy):
        
        self.strategy = _strategy
        
        self.solana_client = Client(RPC_API_URL)
        self.solana_asyncClient = AsyncClient(RPC_API_URL)
        
        self.solana_ws = None
        self.ws_healthcheck_interval = 5
        self.ws_connection_active = None
        self.ws_connection_lost_time = datetime(2100,1,1)
        self.manual_disconnect_cnt = 0
        
        self.sub_to_acc_map = {}
        self.sub_to_txid_map = {}
        self.req_to_acc_map = {}
        self.req_to_tx_map = {}
        self.req_counter = 0
        
        self.home_wallet = HOME_WALLET_PUBKEY
        self.home_wallet_pubkey = Pubkey.from_string(HOME_WALLET_PUBKEY)
        self.home_wallet_priv = HOME_WALLET_PRIVATE_KEY
        self.home_wallet_privkey = Keypair.from_bytes(base58.b58decode(HOME_WALLET_PRIVATE_KEY))
        
        self.tracked_wallets = TRACKED_WALLET_PUBKEYS
        self.tracked_wallet_pubkeys = {}
        for pkey in self.tracked_wallets:
            self.tracked_wallet_pubkeys[pkey] = Pubkey.from_string(pkey)
                
        self.balances = {}
        self.last_tx_time = {}
        self.last_tx_sig = {}
        self.tx_details_mapping = {}
    
    def start_ws(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.ws_main())
        
    async def connect_ws(self):
        if self.solana_ws is None or self.solana_ws.closed:
            
            self.req_counter = 0
            self.solana_ws = await websockets.connect(RPC_WSS_URL)
            self.strategy.print_msg("Connected to WebSocket")
            
            latest_time = datetime.utcfromtimestamp(time.time())
            time_since_last_connection = latest_time - self.ws_connection_lost_time
            if time_since_last_connection.days == 0 and time_since_last_connection.seconds < 72000:
                self.strategy.print_msg('Processing any missed transactions...')
                self.strategy.process_missed_transactions()
            
            for address in self.tracked_wallets:
                signatures = self.solana_client.get_signatures_for_address(self.tracked_wallet_pubkeys[address], limit=1, commitment='confirmed')
                signatures = json.loads(signatures.to_json())
                if 'result' in signatures and len(signatures['result']) > 0:
                    self.last_tx_sig[address] = signatures['result'][0]['signature']
                    self.last_tx_time[address] = datetime.utcfromtimestamp(signatures['result'][0]['blockTime'])
            
            self.strategy.save_last_txid()
            
    async def close_ws(self):
        if self.solana_ws is not None and not self.solana_ws.closed:
            await self.solana_ws.close()
            self.strategy.print_msg("WebSocket connection closed")
    
    async def ws_main(self):
        while True:
            try:
                
                await self.connect_ws()
                
                for wallet in self.tracked_wallets:
                    await self.subscribe_to_wallet_transactions(wallet)
                                
                tasks = [
                    asyncio.create_task(self.websocket_health_check()),
                    asyncio.create_task(self.handle_messages())
                ]
                
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                
                # If one of the tasks (likely handle_messages) finishes, cancel the others
                for task in pending:
                    task.cancel()
                
                for task in done:
                    if task.exception():
                        raise task.exception()
                        
                ### Make a new transaction here and sleep for some seconds ####--------------------------------
                
                print('Waiting for dummy transaction when websocket disconnects...')
                await asyncio.sleep(0) #was 60 seconds

                # ---------------------------------------------------------------------------------------------
            
            except websockets.ConnectionClosed:
                self.strategy.print_msg("Websocket connection lost. Restarting...")
                self.ws_connection_lost_time = datetime.utcfromtimestamp(time.time())
                await asyncio.sleep(1)
            except Exception as e:
                self.strategy.handle_exception('ws_main', e)
                self.strategy.print_msg("Error occurred. Reconnecting websocket...")
                self.ws_connection_lost_time = datetime.utcfromtimestamp(time.time())
                await asyncio.sleep(1)  # Wait before reconnecting
            finally:
                await self.cleanup()
    
    async def cleanup(self):
        if self.solana_ws and not self.solana_ws.closed:
            await self.solana_ws.close()
    
    async def subscribe_to_wallet(self, address):
        
        self.req_counter += 1
        self.req_to_acc_map[self.req_counter] = address
        
        # Create the subscription request
        subscription_request = json.dumps({
            "jsonrpc": "2.0",
            "id": self.req_counter,
            "method": "accountSubscribe",
            "params": [address, {"encoding": "jsonParsed", "commitment": "finalized"}]
        })
        
        # Send the subscription request
        await self.solana_ws.send(subscription_request)
        
        message = await self.solana_ws.recv()
        data = json.loads(message)
        
        if 'result' in data and 'id' in data:
            subscription_id = data['result']
            self.sub_to_acc_map[subscription_id] = address
            self.strategy.print_msg(f"Subscribed to wallet: {address}")
            
    async def subscribe_to_wallet_transactions(self, address):
        
        self.req_counter += 1
        self.req_to_acc_map[self.req_counter] = address
        
        subscription_request = json.dumps({
            "jsonrpc": "2.0",
            "id": self.req_counter,
            "method": "logsSubscribe",
            "params": [{"mentions": [ address ]},
                       {"commitment": "confirmed"}]
        })
        
        await self.solana_ws.send(subscription_request)
        
        message = await self.solana_ws.recv()
        data = json.loads(message)
        
        if 'result' in data and 'id' in data:
            subscription_id = data['result']
            self.sub_to_acc_map[subscription_id] = address
            self.strategy.print_msg(f"Subscribed to wallet transactions: {address}")
    
    async def simulate_connection_closed(self, delay):
        print(f'Websocket will disconnect in {delay} seconds')
        await asyncio.sleep(delay)
    
    
    async def handle_messages(self):
        while True:
            try:
                if self.manual_disconnect_cnt < 1:
                    # Create tasks for receiving messages and simulating connection closure
                    recv_task = asyncio.create_task(self.solana_ws.recv())
                    simulate_error_task = asyncio.create_task(self.simulate_connection_closed(1))

                    done, pending = await asyncio.wait(
                        [recv_task, simulate_error_task],
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    # Cancel any pending tasks
                    for task in pending:
                        task.cancel()

                    # Process completed tasks and handle exceptions
                    for task in done:
                        try:
                            if task.exception():
                                exc = task.exception()
                                if isinstance(exc, websockets.ConnectionClosedError):
                                    # Explicit handling for ConnectionClosedError
                                    self.strategy.print_msg("Websocket connection closed unexpectedly.")
                                    self.ws_connection_lost_time = datetime.utcfromtimestamp(time.time())
                                    await self.close_ws()  # Cleanup
                                    raise exc  # Re-raise to trigger reconnection in ws_main
                                else:
                                    # Handle other exceptions as needed
                                    self.strategy.handle_exception('handle_messages', exc)
                                    raise exc
                            else:
                                result = await task  # <--- This line ensures any exceptions are raised and logged.
                        except Exception as e:
                            print(f"{datetime.utcnow()} - Future exception: {e}")
                            self.strategy.handle_exception('handle_messages', e)
                        finally:
                            # Ensure any resources in task are released
                            task.cancel()

                    # If simulate_error_task completed, simulate disconnect
                    if simulate_error_task in done:
                        print('Disconnecting now')
                        self.manual_disconnect_cnt += 1
                        raise websockets.ConnectionClosedError(1000, 'Simulated connection closed for testing')

                    # If recv_task completed successfully, process the received message
                    if recv_task in done:
                        message = await recv_task
                        data = json.loads(message)

                        if 'result' in data and 'id' in data:
                            subscription_id = data['result']
                            request_id = data['id']
                            address = self.req_to_acc_map.pop(request_id, None)
                            if address and address != 'transaction':
                                self.sub_to_acc_map[subscription_id] = address
                                self.strategy.print_msg(f"Subscribed to wallet: {address}")
                            elif address and address == 'transaction':
                                txid = self.req_to_tx_map[request_id]
                                self.sub_to_txid_map[subscription_id] = txid
                                self.strategy.print_msg(f"Subscribed to transaction ID: {txid}")
                            else:
                                print(data)
                                print(self.req_counter)
                                print(f"A subscription seems to have failed - {subscription_id}")
                        # Dispatch message based on its type
                        elif 'method' in data and data['method'] == 'logsNotification':
                            await self.process_wallet_transactions_message(data)
                        elif 'method' in data and data['method'] == 'accountNotification':
                            await self.process_wallet_message(data)
                        elif 'method' in data and data['method'] == 'signatureNotification':
                            await self.process_signature_message(data)

                else:
                    # Recheck connection status before every receive call to avoid lingering tasks
                    if not self.solana_ws.open:
                        await self.connect_ws()  # Reconnect if closed

                    # Receive and parse the message
                    message = await self.solana_ws.recv()
                    data = json.loads(message)

                    if 'result' in data and 'id' in data:
                        subscription_id = data['result']
                        request_id = data['id']
                        address = self.req_to_acc_map.pop(request_id, None)
                        if address and address != 'transaction':
                            self.sub_to_acc_map[subscription_id] = address
                            self.strategy.print_msg(f"Subscribed to wallet: {address}")
                        elif address and address == 'transaction':
                            txid = self.req_to_tx_map[request_id]
                            self.sub_to_txid_map[subscription_id] = txid
                            self.strategy.print_msg(f"Subscribed to transaction ID: {txid}")
                        else:
                            print(data)
                            print(self.req_counter)
                            print(f"A subscription seems to have failed - {subscription_id}")
                    # Dispatch message based on its type
                    elif 'method' in data and data['method'] == 'logsNotification':
                        await self.process_wallet_transactions_message(data)
                    elif 'method' in data and data['method'] == 'accountNotification':
                        await self.process_wallet_message(data)
                    elif 'method' in data and data['method'] == 'signatureNotification':
                        await self.process_signature_message(data)

            except websockets.ConnectionClosedError:
                # Ensure explicit cleanup on connection closed
                self.strategy.print_msg("Websocket connection closed. Reconnecting...")
                self.ws_connection_lost_time = datetime.utcfromtimestamp(time.time())
                await self.close_ws()
                break  # Reconnect in ws_main

            except Exception as e:
                # Handle unexpected exceptions explicitly
                print(f"{datetime.utcnow()} - Future exception was never received: {e}")
                self.strategy.print_msg("Error in message handling")
                self.strategy.handle_exception('handle_messages', e)




    
    async def process_wallet_transactions_message(self, data):
        
        subscription_id = data['params']['subscription']
        address = self.sub_to_acc_map[subscription_id]
        if not address:
            print(f"New transaction unknown subscription ID: {subscription_id}")
            return
        
        try:
            err = data['params']['result']['value']['err']
        except KeyError:
            err = 'unknown'
        
        if err == None:
            
            txid = data['params']['result']['value']['signature']
            transaction_ts = datetime.utcfromtimestamp(time.time())
            
            self.last_tx_time[address] = transaction_ts
            self.last_tx_sig[address] = txid
            
            Thread(target=self.strategy.save_last_txid).start()
            
            self.strategy.print_msg(f'{address} - New transaction detected - {txid}')

            n = 0
            while n < 10:
                try:
                    tx = self.solana_client.get_transaction(Signature.from_string(txid), commitment='confirmed', max_supported_transaction_version=0)
                    tx = json.loads(tx.to_json())
                    if 'result' in tx and tx['result'] != None and 'transaction' in tx['result'] and 'message' in tx['result']['transaction']:
                        break
                    else:
                        n += 1
                except SolanaRpcException:
                    n += 1
                    continue
            
            if n >= 10:
                self.strategy.print_msg(f'{address} - Unable to retrieve transaction info. Skipping. - {txid}')
            
            else:
                self.strategy.log_msg(f"{transaction_ts} - ws - {address} - Transaction involving {address}: {data}", 'INFO')
                Thread(target=self.strategy.run_strategy, args=(address, transaction_ts, tx, txid,)).start()
    
    async def process_wallet_message(self, data):
        
        subscription_id = data['params']['subscription']
        address = self.sub_to_acc_map[subscription_id]
        if not address:
            print(f"New transaction unknown subscription ID: {subscription_id}")
            return
        
        self.strategy.print_msg(f'{address} - New transaction detected')
        
        transaction_ts = datetime.utcfromtimestamp(time.time())
        self.strategy.log_msg(f"{transaction_ts} - ws - {address} - Transaction involving {address}: {data}", 'INFO')
        
        Thread(target=self.strategy.run_strategy, args=(address, transaction_ts,)).start()
        
    async def process_signature_message(self, data):
        subscription_id = data['params']['subscription']
        txid = self.sub_to_txid_map[subscription_id]
        if not txid:
            print(f"Unknown subscription ID for transaction: {txid}")
            return    
        
        confirmation = 'confirmed' if data['params']['result']['value']['err'] == None else data['params']['result']['value']['err']
        confirmation_ts = datetime.utcfromtimestamp(time.time())
        self.strategy.log_msg(f"{txid} - Transaction confirmation: {data}", 'INFO')
        Thread(target=self.strategy.process_confirmed_transaction, args=(txid, confirmation_ts, confirmation,)).start()
    
    async def websocket_health_check(self):
        while True:
            try:
                # Send a ping to the server to check if the connection is still alive
                await self.solana_ws.ping()
                self.ws_connection_active = True
            except Exception as e:
                self.strategy.print_msg(f"WebSocket health check failed: {e}")
                self.strategy.handle_exception('websocket_health_check', e)
                self.ws_connection_active = False
                break  # Exit the loop if the connection is broken
            # Wait for the next health check
            await asyncio.sleep(self.ws_healthcheck_interval)
    
    def fetch_transaction_details(self, slot):
        # Use the slot (block number) from the WebSocket notification to query the transaction
        block_info = self.solana_client.get_block(slot, max_supported_transaction_version=0)
        
        # List of transaction signatures
        signatures = block_info.value.transactions
        
        select_transaction = [s for s in signatures if self.home_wallet_pubkey in s.transaction.message.account_keys]
        
        for signature_info in signatures:
            # Extract the transaction signature
            signature = signature_info.transaction.signatures[0]
            
            # Fetch full transaction details using the signature
            transaction_details = self.solana_client.get_confirmed_transaction(signature)
            
            print(f"Transaction Details for signature {signature}:")
            print(transaction_details)
    
    def get_wallet_balances(self):
        start = time.time()
        for wallet_pubkey_str, wallet_pubkey in {self.home_wallet: self.home_wallet_pubkey}.items():
            saved_balance = self.balances[wallet_pubkey_str] if wallet_pubkey_str in self.balances else {}
            self.get_wallet_balance(wallet_pubkey_str, wallet_pubkey)
            if self.balances[wallet_pubkey_str] != saved_balance:
                self.strategy.log_msg(f'{datetime.utcfromtimestamp(time.time())} - api - {wallet_pubkey_str} - {self.balances[wallet_pubkey_str]}', 'INFO')
        self.strategy.print_msg(f'Starting wallet balances retrieved. Time taken: {round(time.time() - start, 2)}s')
    
    def get_wallet_balance(self, wallet_pubkey_str, wallet_pubkey):
        sol_balance = self.get_sol_balance(wallet_pubkey)
        spl_balances = self.get_spl_token_balances(wallet_pubkey)
        self.balances[wallet_pubkey_str] = {**sol_balance, **spl_balances}
    
    def get_sol_balance(self, wallet_pubkey, error_cnt=0):
        try:
            balance = self.solana_client.get_balance(wallet_pubkey)
        except SolanaRpcException as e:
            if error_cnt < 5:
                time.sleep(0.2)
                error_cnt += 1
                return self.get_sol_balance(wallet_pubkey, error_cnt=error_cnt)
            else:
                print(e)
        balance = balance.value / 1000000000        
        return {'So11111111111111111111111111111111111111112': balance}
    
    def get_spl_token_balances(self, wallet_pubkey):
        # Fetch all token accounts for the wallet
        token_accounts = self.solana_client.get_token_accounts_by_owner_json_parsed(wallet_pubkey, TokenAccountOpts(program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))).value
        
        spl_balances = {}
        # Iterate over token accounts and fetch balances
        for account in token_accounts:
            # token_account_pubkey = account.pubkey
            token_mint = account.account.data.parsed['info']['mint']
            token_balance = account.account.data.parsed['info']['tokenAmount']['uiAmount']
            spl_balances[token_mint] = token_balance
            
        return spl_balances
    
    # async def get_wallet_balances(self):
    #     sol_balance_task = asyncio.create_task(self.get_sol_balance(self.home_wallet_pubkey))
    #     spl_balances_task = asyncio.create_task(self.get_spl_token_balances(self.home_wallet_pubkey))
        
    #     sol_balance, spl_balances = await asyncio.gather(sol_balance_task, spl_balances_task)
        
    #     balances = {**sol_balance, **spl_balances}
    #     return balances
    
    # async def get_sol_balance(self, wallet_pubkey):
    #     balance_response = await self.solana_asyncClient.get_balance(wallet_pubkey)
    #     balance = balance_response.value / 1000000000        
    #     return {'So11111111111111111111111111111111111111112': balance}
    
    # async def get_spl_token_balances(self, wallet_pubkey):
    #     # Fetch all token accounts for the wallet
    #     token_account_opts = TokenAccountOpts(program_id=Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"))
    #     token_accounts_response = await self.solana_asyncClient.get_token_accounts_by_owner_json_parsed(wallet_pubkey, token_account_opts)
    #     token_accounts = token_accounts_response.value
        
    #     spl_balances = {}
    #     # Iterate over token accounts and fetch balances
    #     for account in token_accounts:
    #         token_mint = account.account.data.parsed['info']['mint']
    #         token_balance = account.account.data.parsed['info']['tokenAmount']['uiAmount']
    #         spl_balances[token_mint] = token_balance
            
    #     return spl_balances
    
    def wallet_balances_stream(self):
        while True:
            self.get_wallet_balances()
            time.sleep(1)
    
    def get_jupiter_quote(self, input_mint, output_mint, amount, slippage_bps):
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount={amount}&slippageBps={slippage_bps}"
        quote_response = requests.get(url=quote_url).json()
        return quote_response
    
    def get_jupiter_price_vs_sol(self, token_mint, amount_in_sol):
        
        sol_decimals = 9
        token_decimals = self.get_token_decimals(token_mint)
        
        sol_mint = 'So11111111111111111111111111111111111111112'
        amount_in_lamports = int(amount_in_sol * 10**sol_decimals)
        
        quote_url = f"https://quote-api.jup.ag/v6/quote?inputMint={sol_mint}&outputMint={token_mint}&amount={amount_in_lamports}"
        quote_response = requests.get(url=quote_url).json()
        
        price = (int(quote_response['inAmount']) / (10**sol_decimals)) / (int(quote_response['outAmount']) / (10**token_decimals))
        
        return price
    
    def get_swap_route(self, quote_response, prioritization_fee):
        
        # Prepare swap payload
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": self.home_wallet,
            "wrapUnwrapSOL": True, 
            "prioritizationFeeLamports": prioritization_fee
        }
        swap_url = "https://quote-api.jup.ag/v6/swap"
        swap_route = requests.post(url=swap_url, json=payload).json()['swapTransaction']
        return swap_route
    
    def get_token_decimals(self, token_mint):
        response = self.solana_client.get_account_info(Pubkey.from_string(token_mint))
        response_json = json.loads(response.to_json())
        token_data = base64.b64decode(response_json['result']['value']['data'][0])
        decimals = struct.unpack_from("<B", token_data, offset=44)[0]
        return decimals
    
    def swap_via_jupiter(self, input_mint, output_mint, amount, slippage_bps, prioritization_fee):
        
        decimals = self.get_token_decimals(input_mint)
        amount = int(amount * (10**decimals))


    
        
        quote_response = self.get_jupiter_quote(input_mint, output_mint, amount, slippage_bps)
        if 'error' in quote_response:
            self.strategy.print_msg(quote_response['error'])
            raise
        swap_route = self.get_swap_route(quote_response, prioritization_fee)
            
        # Decode and sign the transaction
        raw_transaction = VersionedTransaction.from_bytes(base64.b64decode(swap_route))
        signature = self.home_wallet_privkey.sign_message(message.to_bytes_versioned(raw_transaction.message))
        signed_txn = VersionedTransaction.populate(raw_transaction.message, [signature])
        

        opts = TxOpts(skip_preflight=True, preflight_commitment=Processed)
        
        error = False
        n = 0
        while n < 10:
            try:
                if n == 1:
                    opts = TxOpts(skip_preflight=True, preflight_commitment=Processed)
                result = self.solana_client.send_raw_transaction(txn=bytes(signed_txn), opts=opts)
                error = False
                break
            except SolanaRpcException:
                self.strategy.log_msg('Error while sending transaction: SolanaRpcException. Retrying...', 'ERROR')
                pass
            except Exception as e:
                if 'SendTransactionPreflightFailureMessage' in str(e):
                    self.strategy.log_msg(e, 'ERROR')
                else:
                    self.strategy.handle_exception('swap_via_jupiter', e)
                error = True
                n += 1
        
        if not error:
            txid = json.loads(result.to_json())['result']
            
            # Log the transaction ID and blockhash used
            self.strategy.print_msg(f'Copy transaction sent - {amount} {input_mint} to {output_mint}')      
            asyncio.run(self.subscribe_to_transaction(txid))
            
        else:
            txid = None
            self.strategy.print_msg(f'Copy transaction failed - {amount} {input_mint} to {output_mint}')
        
        return txid

    
    def poll_tx_for_status(self, txid):
        
        # Poll for processing status
        tx_success = False
        for attempt in range(30):
            try:
                status_response = self.solana_client.get_signature_statuses([Signature.from_string(txid)])
            except SolanaRpcException:
                time.sleep(2)
                continue
            status_response = json.loads(status_response.to_json())
            if status_response['result']['value'][0]:
                if status_response['result']['value'][0]['confirmationStatus'] in ['processed', 'confirmed', 'finalized']:
                    tx_success = True
                    break
                else:
                    tx_success = False
            else:
                tx_success = False
            time.sleep(2)
        
        if tx_success == False:
            return False
        else:
            return True
        
    async def subscribe_to_transaction(self, txid):
                
        self.req_counter += 1
        self.req_to_acc_map[self.req_counter] = 'transaction'
        self.req_to_tx_map[self.req_counter] = txid
        
        # Subscribe to the transaction ID
        subscription_request = {
            "jsonrpc": "2.0",
            "id": self.req_counter,
            "method": "signatureSubscribe",
            "params": [txid, {"commitment": "confirmed"}]
        }
        
        await self.solana_ws.send(json.dumps(subscription_request))
            
    def check_transaction_status(self, txid):
        tx_details = self.solana_client.get_transaction(txid, max_supported_transaction_version=0)
    
if __name__ == '__main__':
    bridge = Bridge(None)
    # Thread(target=bridge.start_ws).start()
    # bridge.wallet_balances_stream()
    
    # try:
    #     while True:
    #         time.sleep(1)  # Keep the main thread alive
    # except KeyboardInterrupt:
    #     print("Exiting...")
    
    input_mint = 'CFzhqSNqYZRsUszCGwZ3SJ9iPHLvSumffaS6gWuupump'
    output_mint = 'So11111111111111111111111111111111111111112'
    # input_mint = 'So11111111111111111111111111111111111111112'
    # output_mint = 'JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN'
    # amount = 0.5
    
    # txid = bridge.swap_via_jupiter(input_mint, output_mint, amount)
    # print(txid)
    # print(f'Time now: {datetime.utcfromtimestamp(time.time())}')
    
    price = bridge.get_jupiter_price_vs_sol(input_mint, 0.001)