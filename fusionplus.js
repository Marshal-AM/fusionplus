const express = require('express');
const bodyParser = require('body-parser');
const {
    HashLock,
    NetworkEnum,
    OrderStatus,
    PresetEnum,
    PrivateKeyProviderConnector,
    SDK
} = require('@1inch/cross-chain-sdk');
const { Web3 } = require('web3');
const { randomBytes } = require('crypto');
const dotenv = require('dotenv');

// Load environment variables
dotenv.config();

const app = express();
const PORT = process.env.PORT || 3000;
const authKey = process.env.AUTH_KEY;
const source = process.env.SOURCE || 'sdk-tutorial';

// Middleware
app.use(bodyParser.json());

// Helper to map chain names to NetworkEnum values
function getChainId(chainName) {
    if (!chainName) return null;
    
    // Convert to uppercase for case-insensitive comparison
    const name = chainName.toUpperCase();
    
    // Return the corresponding NetworkEnum value
    return NetworkEnum[name];
}

// Sleep function to add delay between API requests
async function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

// Function to retry an API call with exponential backoff
async function retryWithBackoff(fn, maxRetries = 5, initialDelay = 3000) {
    let retries = 0;
    let delay = initialDelay;

    while (true) {
        try {
            return await fn();
        } catch (error) {
            if (retries >= maxRetries) {
                console.error(`Maximum retries (${maxRetries}) reached. Giving up.`);
                throw error;
            }

            const isRateLimit = 
                error?.response?.data === 'The limit of requests per second has been exceeded' ||
                error?.message?.includes('rate limit') ||
                error?.message?.includes('too many requests');

            if (!isRateLimit) {
                throw error; // If it's not a rate limit error, don't retry
            }

            retries++;
            console.warn(`Rate limit exceeded. Retry ${retries}/${maxRetries} after ${delay}ms delay...`);
            await sleep(delay);
            delay *= 2; // Exponential backoff: double the delay each time
        }
    }
}

// Cross-chain swap endpoint
app.post('/api/swap', async (req, res) => {
    try {
        const {
            privateKey,
            amount = '1000000',
            srcChain = 'COINBASE',  // Use chain names instead of IDs
            dstChain = 'ARBITRUM',  // Use chain names instead of IDs
            srcTokenAddress = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913', // Default USDC
            dstTokenAddress = '0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9', // Default USDT
            rpc = 'https://ethereum-rpc.publicnode.com'
        } = req.body;

        // Validate required fields
        if (!privateKey) {
            return res.status(400).json({
                success: false,
                message: 'Missing required parameter: privateKey'
            });
        }

        // Convert chain names to NetworkEnum values
        const srcChainId = getChainId(srcChain);
        const dstChainId = getChainId(dstChain);
        
        // Validate chain names
        if (!srcChainId) {
            return res.status(400).json({
                success: false,
                message: `Invalid source chain name: ${srcChain}. Please use values like ETHEREUM, ARBITRUM, POLYGON, COINBASE, etc.`
            });
        }
        
        if (!dstChainId) {
            return res.status(400).json({
                success: false,
                message: `Invalid destination chain name: ${dstChain}. Please use values like ETHEREUM, ARBITRUM, POLYGON, COINBASE, etc.`
            });
        }

        // Initialize web3 and get wallet address
        const web3 = new Web3(rpc);
        const account = web3.eth.accounts.privateKeyToAccount(privateKey);
        const walletAddress = account.address;

        // Initialize SDK
        const sdk = new SDK({
            url: 'https://api.1inch.dev/fusion-plus',
            authKey,
            blockchainProvider: new PrivateKeyProviderConnector(privateKey, web3)
        });

        console.log('Starting cross-chain swap process...');
        console.log(`Wallet address: ${walletAddress}`);
        console.log(`Source chain: ${srcChain} (${srcChainId})`);
        console.log(`Destination chain: ${dstChain} (${dstChainId})`);
        
        // Add initial delay
        await sleep(2000);
        
        // Get quote with retry logic
        console.log('Getting quote...');
        const quote = await retryWithBackoff(() => sdk.getQuote({
            amount,
            srcChainId,
            dstChainId,
            enableEstimate: true,
            srcTokenAddress,
            dstTokenAddress,
            walletAddress
        }));
        
        console.log('Quote received');
        await sleep(5000);
        
        const preset = PresetEnum.fast;
        
        // Generate secrets
        console.log('Generating secrets...');
        const secrets = Array.from({
            length: quote.presets[preset].secretsCount
        }).map(() => '0x' + randomBytes(32).toString('hex'));
        
        const hashLock =
            secrets.length === 1
                ? HashLock.forSingleFill(secrets[0])
                : HashLock.forMultipleFills(HashLock.getMerkleLeaves(secrets));
        
        const secretHashes = secrets.map((s) => HashLock.hashSecret(s));
        console.log(`Generated ${secrets.length} secrets`);
        
        await sleep(5000);
        
        // Create order with retry logic
        console.log('Creating order...');
        const { hash, quoteId, order } = await retryWithBackoff(() => sdk.createOrder(quote, {
            walletAddress,
            hashLock,
            preset,
            source,
            secretHashes
        }));
        console.log({ hash }, 'Order created');
        
        await sleep(7000);
        
        // Submit order with retry logic
        console.log('Submitting order...');
        await retryWithBackoff(() => sdk.submitOrder(
            quote.srcChainId,
            order,
            quoteId,
            secretHashes
        ));
        console.log({ hash }, 'Order submitted');
        
        // Start tracking process in the background
        processOrder(sdk, hash, secrets);
        
        // Return successful response with order hash for tracking
        return res.status(200).json({
            success: true,
            message: 'Swap order initiated successfully',
            data: {
                orderHash: hash,
                walletAddress,
                amount,
                sourceChain: srcChain,
                destinationChain: dstChain,
                sourceToken: srcTokenAddress,
                destinationToken: dstTokenAddress
            }
        });
        
    } catch (error) {
        console.error('Error processing swap:', error);
        return res.status(500).json({
            success: false,
            message: 'Error processing swap',
            error: error.message || 'Unknown error'
        });
    }
});

// Helper endpoint to list all available chains
app.get('/api/chains', (req, res) => {
    try {
        // Get all chain names from the NetworkEnum
        const chains = Object.keys(NetworkEnum)
            .filter(key => isNaN(Number(key))) // Filter out numeric keys
            .map(chainName => ({
                name: chainName,
                id: NetworkEnum[chainName]
            }));
        
        return res.status(200).json({
            success: true,
            data: {
                chains
            }
        });
    } catch (error) {
        return res.status(500).json({
            success: false,
            message: 'Error retrieving chain list',
            error: error.message
        });
    }
});

// Order status endpoint
app.get('/api/status/:orderHash', async (req, res) => {
    try {
        const { orderHash } = req.params;
        const { privateKey, rpc = 'https://ethereum-rpc.publicnode.com' } = req.query;
        
        if (!privateKey) {
            return res.status(400).json({
                success: false,
                message: 'Missing required parameter: privateKey'
            });
        }
        
        // Initialize web3 and SDK
        const web3 = new Web3(rpc);
        const sdk = new SDK({
            url: 'https://api.1inch.dev/fusion-plus',
            authKey,
            blockchainProvider: new PrivateKeyProviderConnector(privateKey, web3)
        });
        
        const statusResponse = await retryWithBackoff(() => sdk.getOrderStatus(orderHash));
        
        return res.status(200).json({
            success: true,
            data: statusResponse
        });
        
    } catch (error) {
        console.error('Error getting order status:', error);
        return res.status(500).json({
            success: false,
            message: 'Error getting order status',
            error: error.message || 'Unknown error'
        });
    }
});

// Background function to process the order
async function processOrder(sdk, hash, secrets) {
    try {
        await sleep(10000);
        
        console.log('Monitoring order status...');
        let attempts = 0;
        const maxAttempts = 20;  // Limit the number of attempts

        while (attempts < maxAttempts) {
            attempts++;
            console.log(`Status check attempt ${attempts}/${maxAttempts}`);
            
            if (attempts > 1) {
                await sleep(10000);
            }
            
            let secretsToShare;
            try {
                secretsToShare = await retryWithBackoff(() => sdk.getReadyToAcceptSecretFills(hash));
            } catch (error) {
                console.error('Error checking for ready fills:', error);
                continue;
            }
            
            if (secretsToShare.fills.length) {
                console.log(`Found ${secretsToShare.fills.length} fills ready to accept`);
                
                for (const { idx } of secretsToShare.fills) {
                    console.log(`Submitting secret for index ${idx}...`);
                    try {
                        await retryWithBackoff(() => sdk.submitSecret(hash, secrets[idx]));
                        console.log({ idx }, 'Shared secret');
                    } catch (error) {
                        console.error(`Failed to submit secret for index ${idx}:`, error);
                    }
                    
                    await sleep(10000);
                }
            } else {
                console.log('No fills ready to accept yet');
            }
            
            await sleep(5000);
            
            let statusResponse;
            try {
                statusResponse = await retryWithBackoff(() => sdk.getOrderStatus(hash));
                console.log(`Current order status: ${statusResponse.status}`);
                
                if (
                    statusResponse.status === OrderStatus.Executed ||
                    statusResponse.status === OrderStatus.Expired ||
                    statusResponse.status === OrderStatus.Refunded
                ) {
                    console.log(`Order has reached final status: ${statusResponse.status}`);
                    break;
                }
            } catch (error) {
                console.error('Error checking order status:', error);
            }
        }
        
        await sleep(5000);
        
        try {
            const finalStatusResponse = await retryWithBackoff(() => sdk.getOrderStatus(hash));
            console.log('Final order status:');
            console.log(finalStatusResponse);
        } catch (error) {
            console.error('Error getting final status:', error);
        }
    } catch (error) {
        console.error('Error in processOrder:', error);
    }
}

// Start server
app.listen(PORT, () => {
    console.log(`1inch Cross-Chain API running on port ${PORT}`);
    console.log(`Use /api/chains to get a list of supported blockchain networks`);
});
