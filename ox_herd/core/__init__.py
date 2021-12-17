"""Core modules for ox_herd.
"""

import os


REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost') 
