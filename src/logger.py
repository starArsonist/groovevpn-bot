import sys
from loguru import logger

def setup_logger():
    # Remove default logger
    logger.remove()
    
    # Add a custom logger to stdout
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",
        enqueue=True,
    )
    
    logger.info("Logger initialized.")
