"""
Hot reload support for Ouroboros.

This module provides functions for hot reloading Python modules
without full process restart.
"""

import importlib
import logging
import sys
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def reload_ouroboros_modules() -> bool:
    """Reload all ouroboros modules in dependency order.
    
    Returns True if successful, False on error.
    """
    try:
        # Core modules in dependency order
        import ouroboros
        importlib.reload(ouroboros.utils)
        importlib.reload(ouroboros.llm)
        importlib.reload(ouroboros.loop)
        importlib.reload(ouroboros.context)
        importlib.reload(ouroboros.memory)
        
        # Tool modules
        import ouroboros.tools
        import pkgutil
        for _, modname, _ in pkgutil.iter_modules(ouroboros.tools.__path__):
            if modname not in ('registry', '__init__'):
                try:
                    mod = importlib.import_module(f'ouroboros.tools.{modname}')
                    importlib.reload(mod)
                except Exception as e:
                    log.warning('Failed to reload tool module %s: %s', modname, e)
        
        importlib.reload(ouroboros.tools.registry)
        importlib.reload(ouroboros.agent)
        
        log.info('All ouroboros modules reloaded')
        return True
        
    except Exception as e:
        log.error('Failed to reload ouroboros modules: %s', e, exc_info=True)
        return False


def reset_chat_agent_singleton() -> bool:
    """Reset the chat agent singleton for hot reload.
    
    Returns True if agent was reset, False if it was not initialized.
    """
    try:
        from supervisor import workers
        
        if workers._chat_agent is None:
            return False
        
        # Close any resources held by the old agent
        agent = workers._chat_agent
        if hasattr(agent, 'tools') and hasattr(agent.tools, '_ctx'):
            ctx = agent.tools._ctx
            if hasattr(ctx, 'browser_state') and ctx.browser_state:
                if ctx.browser_state.browser:
                    try:
                        ctx.browser_state.browser.close()
                    except Exception:
                        pass
        
        workers._chat_agent = None
        log.info('Chat agent singleton reset for hot reload')
        return True
        
    except Exception as e:
        log.warning('Failed to reset chat agent singleton: %s', e)
        return False


def perform_hot_reload(reason: str, ctx: Any) -> bool:
    """Perform a hot reload: reset agent + reload modules.
    
    Args:
        reason: Reason for hot reload (for logging)
        ctx: ToolContext for notifications
        
    Returns:
        True if hot reload succeeded, False if fallback to cold restart needed
    """
    import datetime
    
    try:
        from supervisor import workers
        from ouroboros.utils import append_jsonl
        
        # Check if agent is busy
        if workers._chat_agent is not None and getattr(workers._chat_agent, '_busy', False):
            log.warning('Agent busy, cannot perform hot reload')
            return False
        
        # Reset the agent singleton
        reset_chat_agent_singleton()
        
        # Reload modules
        if not reload_ouroboros_modules():
            log.error('Module reload failed')
            return False
        
        # Log success
        append_jsonl(
            ctx.drive_root / 'logs' / 'supervisor.jsonl',
            {
                'ts': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'type': 'hot_reload',
                'reason': reason,
            },
        )
        
        log.info('Hot reload completed: %s', reason)
        return True
        
    except Exception as e:
        log.error('Hot reload failed: %s', e, exc_info=True)
        return False