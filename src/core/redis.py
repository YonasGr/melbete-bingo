"""
Redis connection and utilities
"""
import redis.asyncio as redis
from typing import Optional
from src.core.config import settings


class RedisClient:
    """Redis client wrapper for async operations"""
    
    def __init__(self):
        self.redis: Optional[redis.Redis] = None
    
    async def connect(self):
        """Connect to Redis"""
        self.redis = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password if settings.redis_password else None,
            decode_responses=True
        )
        await self.redis.ping()
    
    async def close(self):
        """Close Redis connection"""
        if self.redis:
            await self.redis.close()
    
    async def publish(self, channel: str, message: str):
        """Publish message to channel"""
        if self.redis:
            await self.redis.publish(channel, message)
    
    async def subscribe(self, channel: str):
        """Subscribe to channel"""
        if self.redis:
            pubsub = self.redis.pubsub()
            await pubsub.subscribe(channel)
            return pubsub
    
    async def set(self, key: str, value: str, expire: Optional[int] = None):
        """Set key-value"""
        if self.redis:
            await self.redis.set(key, value, ex=expire)
    
    async def get(self, key: str) -> Optional[str]:
        """Get value by key"""
        if self.redis:
            return await self.redis.get(key)
        return None
    
    async def delete(self, key: str):
        """Delete key"""
        if self.redis:
            await self.redis.delete(key)
    
    async def exists(self, key: str) -> bool:
        """Check if key exists"""
        if self.redis:
            return await self.redis.exists(key) > 0
        return False


# Global Redis client instance
redis_client = RedisClient()
