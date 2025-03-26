import requests
from web3 import Web3
import asyncio
import json
import time
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional
from fastapi.middleware.cors import CORSMiddleware

# Hardcoded values
INCH_API_KEY = "yHbA6lIJjSMGMahUz7BOSUwP7EB1oaEz"  # Hardcoded API key
AUTO_APPROVE = True  # Hardcoded auto-approve setting

app = FastAPI(title="1inch Swap API", description="API for performing token swaps via 1inch")

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

class SwapRequest(BaseModel):
    wallet_address: str = Field(..., description="Wallet address to perform the swap from")
    private_key: str = Field(..., description="Private key for the wallet")
    chain_id: int = Field(1, description="Chain ID (1: Ethereum, 10: Optimism, 8453: Base, 42161: Arbitrum)")
    src_token: str = Field(..., description="Source token address")
    dst_token: str = Field(..., description="Destination token address")
    amount: str = Field(..., description="Amount to swap in smallest units (atoms)")
    slippage: float = Field(1.0, description="Slippage tolerance in percentage")

class SwapResponse(BaseModel):
    success: bool
    message: str
    tx_hash: Optional[str] = None
    approval_tx_hash: Optional[str] = None

class InchSwapper:
    def __init__(self, wallet_address, private_key, chain_id):
        self.chain_id = chain_id
        self.api_key = INCH_API_KEY  # Use hardcoded API key
        self.wallet_address = Web3.to_checksum_address(wallet_address)
        self.private_key = private_key
        
        # Map chain IDs to their respective RPC URLs
        chain_rpc_urls = {
            1: "https://eth-mainnet.g.alchemy.com/v2/NMsHzNgJ7XUYtzNyFpEJ8yT4muQ_lkRF",      # Ethereum mainnet
            10: "https://opt-mainnet.g.alchemy.com/v2/NMsHzNgJ7XUYtzNyFpEJ8yT4muQ_lkRF",     # Optimism mainnet
            8453: "https://base-mainnet.g.alchemy.com/v2/NMsHzNgJ7XUYtzNyFpEJ8yT4muQ_lkRF",  # Base mainnet
            42161: "https://arb-mainnet.g.alchemy.com/v2/NMsHzNgJ7XUYtzNyFpEJ8yT4muQ_lkRF"   # Arbitrum mainnet
        }
        
        # Get the appropriate RPC URL for the specified chain ID
        if chain_id not in chain_rpc_urls:
            raise ValueError(f"Unsupported chain ID: {chain_id}. Supported chain IDs are: {list(chain_rpc_urls.keys())}")
        
        rpc_url = chain_rpc_urls[chain_id]
        
        # Set up Web3 with the appropriate provider
        self.web3 = Web3(Web3.HTTPProvider(rpc_url))
        self.api_base_url = f"https://api.1inch.dev/swap/v6.0/{self.chain_id}"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "accept": "application/json"
        }

    def to_checksum_address(self, address):
        """Convert address to checksum format"""
        if isinstance(address, str) and address.startswith('0x'):
            return Web3.to_checksum_address(address.lower())
        return address

    def convert_addresses_to_checksum(self, obj):
        """Recursively convert all addresses in an object to checksum format"""
        if isinstance(obj, dict):
            return {k: self.convert_addresses_to_checksum(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.convert_addresses_to_checksum(item) for item in obj]
        elif isinstance(obj, str) and obj.startswith('0x') and len(obj) == 42:
            try:
                return self.to_checksum_address(obj)
            except:
                return obj
        return obj

    def api_request_url(self, method_name, query_params):
        """Construct full API request URL"""
        # Convert addresses in query parameters to checksum format
        converted_params = {}
        for key, value in query_params.items():
            if key in ['tokenAddress', 'walletAddress', 'src', 'dst', 'from']:
                try:
                    converted_params[key] = self.to_checksum_address(value)
                except:
                    converted_params[key] = value
            else:
                converted_params[key] = value
                
        query_string = '&'.join([f'{key}={value}' for key, value in converted_params.items()])
        return f"{self.api_base_url}{method_name}?{query_string}"

    def check_allowance(self, token_address):
        """Check token allowance for the wallet"""
        try:
            token_address = self.to_checksum_address(token_address)
            url = self.api_request_url("/approve/allowance", {
                "tokenAddress": token_address,
                "walletAddress": self.wallet_address
            })
            
            # Add delay before API call to avoid rate limiting
            time.sleep(1)
            
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            return response.json().get("allowance")
        except requests.exceptions.RequestException as e:
            print(f"Error checking allowance: {str(e)}")
            return "0"

    async def build_tx_for_approve_trade_with_router(self, token_address, amount=None):
        """Build transaction for token approval"""
        try:
            token_address = self.to_checksum_address(token_address)
            params = {"tokenAddress": token_address}
            if amount:
                params["amount"] = amount
                
            url = self.api_request_url("/approve/transaction", params)
            
            # Add delay before API call to avoid rate limiting
            await asyncio.sleep(2)
            
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            transaction = response.json()
            
            # Convert all addresses in transaction to checksum format
            transaction = self.convert_addresses_to_checksum(transaction)
            
            # Ensure critical fields are checksummed
            if 'to' in transaction:
                transaction['to'] = self.to_checksum_address(transaction['to'])
            transaction['from'] = self.wallet_address
            
            # Add necessary transaction fields
            transaction['nonce'] = self.web3.eth.get_transaction_count(self.wallet_address)
            transaction['chainId'] = self.chain_id
            transaction['value'] = 0  # Ensure value field exists
            
            # Estimate gas
            try:
                gas_limit = self.web3.eth.estimate_gas({
                    **transaction,
                    "from": self.wallet_address
                })
                transaction['gas'] = gas_limit
            except Exception as e:
                print(f"Gas estimation failed: {str(e)}")
                transaction['gas'] = 500000  # Fallback gas limit
            
            # Convert all numeric values to integers
            for key in ['gas', 'gasPrice', 'value', 'nonce']:
                if key in transaction and not isinstance(transaction[key], int):
                    transaction[key] = int(transaction[key], 16) if isinstance(transaction[key], str) and transaction[key].startswith('0x') else int(transaction[key])
            
            return transaction
        except Exception as e:
            print(f"Error building approval transaction: {str(e)}")
            raise  # Re-raise the exception for better error tracking

    def build_tx_for_swap(self, swap_params):
        """Build transaction for token swap"""
        try:
            # Convert addresses in swap parameters
            converted_params = swap_params.copy()
            converted_params['src'] = self.to_checksum_address(swap_params['src'])
            converted_params['dst'] = self.to_checksum_address(swap_params['dst'])
            converted_params['from'] = self.to_checksum_address(swap_params['from'])

            url = self.api_request_url("/swap", converted_params)
            
            # Add delay before API call to avoid rate limiting
            time.sleep(3)
            
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()
            transaction = response.json()["tx"]
            
            # Convert all addresses in transaction to checksum format
            transaction = self.convert_addresses_to_checksum(transaction)
            
            # Ensure critical fields are checksummed
            if 'to' in transaction:
                transaction['to'] = self.to_checksum_address(transaction['to'])
            transaction['from'] = self.wallet_address
            
            # Add necessary transaction fields
            transaction['nonce'] = self.web3.eth.get_transaction_count(self.wallet_address)
            transaction['chainId'] = self.chain_id
            
            # Ensure gas is set - fix for "intrinsic gas too low" error
            if 'gas' not in transaction or transaction['gas'] == 0:
                try:
                    # Try to estimate gas
                    gas_limit = self.web3.eth.estimate_gas({
                        **transaction,
                        "from": self.wallet_address
                    })
                    transaction['gas'] = gas_limit
                except Exception as e:
                    print(f"Gas estimation failed: {str(e)}")
                    # Set a higher fallback gas limit for swap transactions
                    transaction['gas'] = 500000  # Higher fallback value for swaps
            
            # Convert all numeric values to integers
            for key in ['gas', 'gasPrice', 'value', 'nonce']:
                if key in transaction and not isinstance(transaction[key], int):
                    transaction[key] = int(transaction[key], 16) if isinstance(transaction[key], str) and transaction[key].startswith('0x') else int(transaction[key])
            
            print(f"Final gas value: {transaction['gas']}")
            return transaction
        except Exception as e:
            print(f"Error building swap transaction: {str(e)}")
            return None

    async def sign_and_send_transaction(self, transaction):
        """Sign and broadcast transaction"""
        try:
            # Double-check gas is present and non-zero
            if 'gas' not in transaction or transaction['gas'] == 0:
                transaction['gas'] = 500000  # Fallback gas value
                print(f"Added fallback gas value: {transaction['gas']}")
                
            signed_tx = self.web3.eth.account.sign_transaction(
                transaction_dict=transaction,
                private_key=self.private_key
            )
            tx_hash = self.web3.eth.send_raw_transaction(signed_tx.raw_transaction)
            return self.web3.to_hex(tx_hash)
        except Exception as e:
            print(f"Error signing/sending transaction: {str(e)}")
            raise

    async def perform_swap(self, src_token, dst_token, amount, slippage=1):
        """Main function to perform the complete swap process"""
        result = {
            "success": False,
            "message": "",
            "tx_hash": None,
            "approval_tx_hash": None
        }
        
        # Convert token addresses to checksum format
        src_token = self.to_checksum_address(src_token)
        dst_token = self.to_checksum_address(dst_token)

        print("Checking token allowance...")
        allowance = self.check_allowance(src_token)
        print(f"Current allowance: {allowance}")

        if int(allowance) < int(amount):
            print("\nInsufficient allowance. An approval transaction is required.")
            
            # Use hardcoded AUTO_APPROVE value
            if AUTO_APPROVE:
                print("\nCreating approval transaction...")
                approval_tx = await self.build_tx_for_approve_trade_with_router(src_token)
                
                if approval_tx:
                    print("Sending approval transaction...")
                    print(f"Nonce: {approval_tx['nonce']}")
                    print(f"Gas limit set to: {approval_tx['gas']}")
                    
                    approve_tx_hash = await self.sign_and_send_transaction(approval_tx)
                    
                    if approve_tx_hash:
                        print(f"Approval transaction hash: {approve_tx_hash}")
                        result["approval_tx_hash"] = approve_tx_hash
                        print("Waiting for approval transaction to be mined...")
                        self.web3.eth.wait_for_transaction_receipt(approve_tx_hash)
                        print("Approval transaction confirmed!")
                    else:
                        result["message"] = "Failed to send approval transaction"
                        return result
                else:
                    result["message"] = "Failed to build approval transaction"
                    return result
            else:
                result["message"] = "Insufficient allowance and auto-approve is disabled"
                return result

        # Add delay before building swap transaction
        await asyncio.sleep(2)

        swap_params = {
            "src": src_token,
            "dst": dst_token,
            "amount": amount,
            "from": self.wallet_address,
            "slippage": slippage,
            "disableEstimate": False,
            "allowPartialFill": True
        }

        print("\nBuilding swap transaction...")
        swap_tx = self.build_tx_for_swap(swap_params)
        
        if swap_tx:
            print(f"Nonce: {swap_tx['nonce']}")
            print(f"Gas limit set to: {swap_tx.get('gas', 'auto')}")
            
            print("Sending swap transaction...")
            swap_tx_hash = await self.sign_and_send_transaction(swap_tx)
            
            if swap_tx_hash:
                print(f"Swap transaction hash: {swap_tx_hash}")
                result["success"] = True
                result["message"] = "Swap completed successfully"
                result["tx_hash"] = swap_tx_hash
                return result
            else:
                result["message"] = "Failed to send swap transaction"
                return result
        else:
            result["message"] = "Failed to build swap transaction"
            return result

@app.post("/swap", response_model=SwapResponse)
async def swap_tokens(swap_request: SwapRequest):
    try:
        swapper = InchSwapper(
            swap_request.wallet_address,
            swap_request.private_key,
            chain_id=swap_request.chain_id
        )
        
        result = await swapper.perform_swap(
            swap_request.src_token,
            swap_request.dst_token,
            swap_request.amount,
            slippage=swap_request.slippage
        )
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Swap failed: {str(e)}")

@app.get("/")
async def root():
    return {"message": "Welcome to 1inch Swap API. Use /swap endpoint to perform token swaps."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000) 
