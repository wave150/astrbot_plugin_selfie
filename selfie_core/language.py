"""Framework-independent character voice generation with explicit LLM dependency."""
import asyncio

async def character_text(profile, instruction, chat_callable, fallback, *, timeout=12):
    if chat_callable is None:
        return fallback
    system = (f'你扮演当前唯一原创角色：{profile.name or "参考图角色"}。'
              f'角色设定：{profile.description or "按当前角色设定"}。'
              f'说话风格：{profile.voice or "自然、简短、口语化"}。'
              '只输出一句角色台词，不添加解释，不改变角色身份，不声称尚未完成的任务已完成。')
    try:
        result = await asyncio.wait_for(chat_callable(system_prompt=system,prompt=instruction),timeout)
        text = str(getattr(result,'completion_text','') or '').strip().strip('“”"')
        return text[:180] if text else fallback
    except asyncio.CancelledError:
        raise
    except Exception:
        return fallback
