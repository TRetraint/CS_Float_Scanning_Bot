import random
import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import json
import os
from datetime import datetime
import logging
from typing import Dict, List, Optional, Set
from enum import Enum
from dotenv import load_dotenv
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SortOption(Enum):
    """Available sorting options for CS Float listings"""
    BEST_DEAL = "best_deal"
    HIGHEST_DISCOUNT = "highest_discount"  
    LOWEST_PRICE = "lowest_price"
    HIGHEST_PRICE = "highest_price"
    MOST_RECENT = "most_recent"
    LOWEST_FLOAT = "lowest_float"
    HIGHEST_FLOAT = "highest_float"
    CREATED_AT = "created_at"

class CSFloatBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        
        # Configuration
        self.csfloat_api_key = os.getenv('CSFLOAT_API_KEY')  # Optional for public endpoints
        self.discord_token = os.getenv('DISCORD_TOKEN')
        self.channel_id = int(os.getenv('CHANNEL_ID', 0))
        
        # Tracking data
        self.seen_listings: Set[str] = set()
        self.tracking_configs: Dict[str, Dict] = {}
        
        # Session for HTTP requests
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def setup_hook(self):
        """Initialize the bot"""
        self.session = aiohttp.ClientSession()
        logger.info("Bot setup completed")
        
    async def close(self):
        """Clean up resources"""
        if self.session:
            await self.session.close()
        await super().close()
        
    async def on_ready(self):
        """Bot ready event"""
        logger.info(f'{self.user} has connected to Discord!')
        if not self.check_listings.is_running():
            self.check_listings.start()
            
    async def fetch_listings(self, **params) -> List[Dict]:
        """Fetch listings from CS Float API"""
        base_url = "https://csfloat.com/api/v1/listings"
        headers = {}
        
        if self.csfloat_api_key:
            headers['Authorization'] = self.csfloat_api_key
            
        try:
            async with self.session.get(base_url, params=params, headers=headers) as response:
                if response.status == 200:
                    response_data = await response.json()
                    # CS Float API returns data wrapped in a 'data' key
                    return response_data.get('data', [])
                else:
                    logger.error(f"API request failed with status {response.status}")
                    logger.error(f"Response: {response}")
                    return []
        except Exception as e:
            logger.error(f"Error fetching listings: {e}")
            return []
    
    def create_listing_embed(self, listing: Dict) -> discord.Embed:
        """Create a Discord embed for a listing"""
        item = listing['item']
        seller = listing['seller']
        
        # Create embed
        embed = discord.Embed(
            title=f"{item['market_hash_name']}",
            url=f"https://csfloat.com/item/{listing['id']}",
            color=discord.Color.blue(),
            timestamp=datetime.fromisoformat(listing['created_at'].replace('Z', '+00:00'))
        )
        
        # Add item details
        price_usd = listing['price'] / 100  # Convert cents to dollars
        embed.add_field(name="💰 Price", value=f"${price_usd:.2f}", inline=True)
        embed.add_field(name="🎯 Float", value=f"{item['float_value']:.6f}", inline=True)
        embed.add_field(name="🎨 Paint Seed", value=item.get('paint_seed', 'N/A'), inline=True)
        
        # Add wear condition
        embed.add_field(name="👕 Condition", value=item.get('wear_name', 'Unknown'), inline=True)
        
        # Add StatTrak/Souvenir info
        special = []
        if item.get('is_stattrak'):
            special.append("StatTrak™")
        if item.get('is_souvenir'):
            special.append("Souvenir")
        if special:
            embed.add_field(name="✨ Special", value=" | ".join(special), inline=True)
        else:
            embed.add_field(name="📊 Rarity", value=f"Grade {item.get('rarity', 'Unknown')}", inline=True)
            
        # Add seller info (handle obfuscated sellers)
        seller_name = seller.get('username', 'Anonymous')
        if not seller_name and seller.get('obfuscated_id'):
            seller_name = f"User {seller['obfuscated_id'][:8]}..."
        embed.add_field(name="👤 Seller", value=seller_name, inline=True)
        
        # Add reference price info and divergence calculation
        reference = listing.get('reference', {})
        if reference:
            predicted_price = reference.get('predicted_price', 0) / 100
            base_price = reference.get('base_price', 0) / 100
            
            if predicted_price > 0:
                embed.add_field(name="📈 Predicted", value=f"${predicted_price:.2f}", inline=True)
                
                # Calculate price divergence percentage
                # Positive = price is higher than predicted (expensive)
                # Negative = price is lower than predicted (discount/deal)
                divergence_pct = ((price_usd - predicted_price) / predicted_price) * 100
                
                # Format divergence with appropriate emoji and color coding
                if divergence_pct > 0:
                    divergence_emoji = "📈"  # Price above prediction
                    divergence_text = f"+{divergence_pct:.1f}%"
                elif divergence_pct < 0:
                    divergence_emoji = "📉"  # Price below prediction (discount)
                    divergence_text = f"{divergence_pct:.1f}%"  # Already has negative sign
                else:
                    divergence_emoji = "⚖️"  # Exact match
                    divergence_text = "0.0%"
                
                embed.add_field(
                    name=f"{divergence_emoji} Divergence", 
                    value=divergence_text, 
                    inline=True
                )
            
            # Keep the old discount field for backward compatibility when price is below predicted
            if base_price > 0 and price_usd < predicted_price:
                discount_pct = ((predicted_price - price_usd) / predicted_price) * 100
                embed.add_field(name="💸 Discount", value=f"{discount_pct:.1f}%", inline=True)
        
        # Add stickers if any
        stickers = item.get('stickers', [])
        if stickers:
            sticker_names = [sticker['name'] for sticker in stickers[:3]]  # Limit to first 3
            sticker_text = "\n".join(sticker_names)
            if len(stickers) > 3:
                sticker_text += f"\n... and {len(stickers) - 3} more"
            embed.add_field(name="🏷️ Stickers", value=sticker_text, inline=False)
        
        # Add watchers if any
        watchers = listing.get('watchers', 0)
        if watchers > 0:
            embed.add_field(name="👀 Watchers", value=str(watchers), inline=True)
        
        # Add item image - handle both old and new icon URL formats
        icon_url = item.get('icon_url', '')
        if icon_url:
            if icon_url.startswith('http'):
                embed.set_thumbnail(url=icon_url)
            elif icon_url.startswith('-9a81'):
                # Old format - prefix with Steam CDN
                embed.set_thumbnail(url=f"https://steamcommunity-a.akamaihd.net/economy/image/{icon_url}")
            else:
                # New format - likely already includes domain or needs different handling
                embed.set_thumbnail(url=f"https://steamcommunity-a.akamaihd.net/economy/image/{icon_url}")
        
        # Add description if available
        description = listing.get('description', '')
        if description:
            embed.add_field(name="📝 Note", value=description[:100] + ("..." if len(description) > 100 else ""), inline=False)
        
        # Add footer
        embed.set_footer(text=f"CS Float • ID: {listing['id']}")
        
        return embed
    
    @tasks.loop(seconds=60)
    async def check_listings(self):
        """Check for new listings periodically"""
        if not self.tracking_configs:
            return
            
        channel = self.get_channel(self.channel_id)
        if not channel:
            logger.warning(f"Channel with ID {self.channel_id} not found")
            return
        
        for config_name, config in self.tracking_configs.items():
            await asyncio.sleep(random.uniform(0, 1))
            try:
                # Fetch listings with the specified parameters
                listings = await self.fetch_listings(**config['params'])
                
                new_listings = []
                for listing in listings:
                    listing_id = listing['id']
                    if listing_id not in self.seen_listings:
                        new_listings.append(listing)
                        self.seen_listings.add(listing_id)
                
                # Send embeds for new listings
                for listing in new_listings[:5]:  # Limit to 5 new items per check
                    embed = self.create_listing_embed(listing)
                    await channel.send(f"🆕 **New {config_name} Listing!**", embed=embed)
                    
                if new_listings:
                    logger.info(f"Posted {len(new_listings)} new listings for {config_name}")
                    
            except Exception as e:
                logger.error(f"Error checking listings for {config_name}: {e}")
    
    @check_listings.before_loop
    async def before_check_listings(self):
        """Wait for bot to be ready before starting the loop"""
        await self.wait_until_ready()

# Bot commands
bot = CSFloatBot()

@bot.command(name='track')
async def track_items(ctx, name: str, def_index: int, paint_index: int, *, params: str = ""):
    """
    Track items with specific parameters
    Usage: !track <name> <def_index> <paint_index> [param1=value1 param2=value2 ...]
    
    Example: !track "AK Redline" 7 282 max_float=0.15 min_float=0.10 max_price=5000 sort_by=highest_discount
    Example: !track "Crimson Kimono" 5034 10033 max_float=0.08 min_price=800000 sort_by=best_deal
    """
    try:
        # Parse additional parameters
        param_dict = {
            'def_index': def_index, 
            'paint_index': paint_index,
            'limit': 20, 
            'sort_by': SortOption.BEST_DEAL.value  # Default sorting
        }
        
        if params:
            for param in params.split():
                if '=' in param:
                    key, value = param.split('=', 1)
                    
                    # Handle sort_by parameter specially
                    if key == 'sort_by':
                        # Validate sort option
                        try:
                            sort_value = SortOption(value).value
                            param_dict[key] = sort_value
                        except ValueError:
                            valid_options = [opt.value for opt in SortOption]
                            await ctx.send(f"❌ Invalid sort option '{value}'. Valid options: {', '.join(valid_options)}")
                            return
                    else:
                        # Convert numeric values
                        try:
                            if '.' in value:
                                param_dict[key] = float(value)
                            else:
                                param_dict[key] = int(value)
                        except ValueError:
                            param_dict[key] = value
        
        # Store tracking configuration
        bot.tracking_configs[name] = {
            'params': param_dict,
            'channel': ctx.channel.id
        }
        
        # Update channel ID if this is the first tracking config
        if len(bot.tracking_configs) == 1:
            bot.channel_id = ctx.channel.id
        
        embed = discord.Embed(
            title="✅ Tracking Started",
            description=f"Now tracking **{name}** items",
            color=discord.Color.green()
        )
        embed.add_field(name="Parameters", value=json.dumps(param_dict, indent=2), inline=False)
        
        await ctx.send(embed=embed)
        
        # Test the configuration by fetching initial listings
        initial_listings = await bot.fetch_listings(**param_dict)
        if initial_listings:
            # Mark existing listings as seen to avoid spam
            for listing in initial_listings:
                bot.seen_listings.add(listing['id'])
            
            embed_copy = discord.Embed(
                title="✅ Tracking Started",
                description=f"Now tracking **{name}** items",
                color=discord.Color.green()
            )
            embed_copy.add_field(name="Parameters", value=json.dumps(param_dict, indent=2), inline=False)
            embed_copy.add_field(
                name="Initial Check", 
                value=f"Found {len(initial_listings)} existing listings (marked as seen)",
                inline=False
            )
            await ctx.send(embed=embed_copy)
        
    except Exception as e:
        await ctx.send(f"❌ Error setting up tracking: {e}")

@bot.command(name='untrack')
async def untrack_items(ctx, name: str):
    """Stop tracking a specific item configuration"""
    if name in bot.tracking_configs:
        del bot.tracking_configs[name]
        await ctx.send(f"✅ Stopped tracking **{name}**")
    else:
        await ctx.send(f"❌ No tracking configuration found for **{name}**")

@bot.command(name='list_tracking')
async def list_tracking(ctx):
    """List all active tracking configurations"""
    if not bot.tracking_configs:
        await ctx.send("No items are currently being tracked.")
        return
    
    embed = discord.Embed(title="📊 Active Tracking Configurations", color=discord.Color.blue())
    
    for name, config in bot.tracking_configs.items():
        params_str = "\n".join([f"{k}: {v}" for k, v in config['params'].items()])
        embed.add_field(name=name, value=f"```{params_str}```", inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name='test')
async def test_fetch(ctx, def_index: int, paint_index: int = None, limit: int = 5, sort_by: str = "best_deal"):
    """Test fetching listings for specific def_index and paint_index"""
    params = {'def_index': def_index, 'limit': limit}
    
    if paint_index:
        params['paint_index'] = paint_index
    
    # Validate sort_by parameter
    try:
        sort_value = SortOption(sort_by).value
        params['sort_by'] = sort_value
    except ValueError:
        valid_options = [opt.value for opt in SortOption]
        await ctx.send(f"❌ Invalid sort option '{sort_by}'. Valid options: {', '.join(valid_options)}")
        return
    
    listings = await bot.fetch_listings(**params)
    
    if not listings:
        await ctx.send(f"❌ No listings found for def_index {def_index}" + (f" paint_index {paint_index}" if paint_index else ""))
        return
    
    await ctx.send(f"✅ Found {len(listings)} listings for def_index {def_index}" + (f" paint_index {paint_index}" if paint_index else "") + f" (sorted by {sort_by})")
    
    # Show first listing as example
    if listings:
        embed = bot.create_listing_embed(listings[0])
        await ctx.send("**Example listing:**", embed=embed)

@bot.command(name='sort_options')
async def sort_options(ctx):
    """Show available sorting options"""
    embed = discord.Embed(
        title="🔄 Available Sort Options",
        description="Use these values for the sort_by parameter",
        color=discord.Color.purple()
    )
    
    sort_descriptions = {
        SortOption.BEST_DEAL: "Best value deals (default)",
        SortOption.HIGHEST_DISCOUNT: "Highest discount from predicted price",
        SortOption.LOWEST_PRICE: "Lowest price first", 
        SortOption.HIGHEST_PRICE: "Highest price first",
        SortOption.MOST_RECENT: "Most recently listed",
        SortOption.LOWEST_FLOAT: "Lowest float value first",
        SortOption.HIGHEST_FLOAT: "Highest float value first",
        SortOption.CREATED_AT: "Sort by creation time"
    }
    
    for option, description in sort_descriptions.items():
        embed.add_field(name=option.value, value=description, inline=False)
    
    embed.add_field(
        name="Example Usage",
        value="```!track 'Crimson Kimono' 5034 10033 sort_by=highest_discount max_float=0.08```",
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name='help_csfloat')
async def help_csfloat(ctx):
    """Show help for CS Float bot commands"""
    embed = discord.Embed(
        title="🤖 CS Float Bot Help",
        description="Track CS:GO items from CS Float market",
        color=discord.Color.blue()
    )
    
    commands_help = {
        "!track <name> <def_index> <paint_index> [params]": "Start tracking items with specific parameters",
        "!untrack <name>": "Stop tracking a configuration",
        "!list_tracking": "List all active tracking configurations", 
        "!test <def_index> [paint_index] [limit] [sort_by]": "Test fetch listings",
        "!sort_options": "Show available sorting options",
        "!help_csfloat": "Show this help message"
    }
    
    for cmd, desc in commands_help.items():
        embed.add_field(name=cmd, value=desc, inline=False)
    
    embed.add_field(
        name="Example Usage",
        value="```!track 'AK Redline' 7 282 max_float=0.15 min_price=1000 max_price=5000 sort_by=highest_discount\n!track 'Crimson Kimono' 5034 10033 max_float=0.08 min_price=800000 sort_by=best_deal```",
        inline=False
    )
    
    embed.add_field(
        name="Common def_index & paint_index pairs",
        value="```AK-47 Redline: def_index=7 paint_index=282\nSpecialist Gloves Crimson Kimono: def_index=5034 paint_index=10033\nSpecialist Gloves Emerald Web: def_index=5034 paint_index=10034\nSpecialist Gloves Foundation: def_index=5034 paint_index=10035```",
        inline=False
    )
    
    embed.add_field(
        name="Available Parameters",
        value="min_float, max_float, min_price, max_price, paint_seed, category, rarity, type, market_hash_name, sort_by",
        inline=False
    )
    
    await ctx.send(embed=embed)

if __name__ == "__main__":    
    if not bot.discord_token:
        logger.error("DISCORD_TOKEN environment variable is required")
        exit(1)
    
    if not bot.channel_id:
        logger.error("CHANNEL_ID environment variable is required")
        exit(1)
    
    try:
        bot.run(bot.discord_token)
    except discord.LoginFailure:
        logger.error("Invalid Discord token")
    except Exception as e:
        logger.error(f"Error running bot: {e}")