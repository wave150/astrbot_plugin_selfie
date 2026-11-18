"""Single-character identity: only a display name.

长相、发型、发色、默认衣服这些都来自用户在工作台「参考图」页上传的图片，
不再有内置默认形象，也不再读 ``character_*`` 的其他字段。配置里只保留
``character_name``：用于称呼、群聊里叫她和照片记忆的归属。
"""
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class CharacterProfile:
    """不可变的角色身份；name 为空表示没起名字。"""

    name: str = ""

    @classmethod
    def from_config(cls, config=None):
        if config is None:
            config = {}
        if not isinstance(config, Mapping):
            raise ValueError("config must be a mapping or None")
        if "character_name" not in config:
            return cls("")
        raw = config.get("character_name")
        if raw is None:
            return cls("")
        if not isinstance(raw, str):
            raise ValueError("character_name must be a string or None")
        return cls(raw.strip())

    @property
    def is_custom(self):
        """兼容旧调用点：现在没有「自定义角色」模式，永远是 False。"""
        return False

    @property
    def aliases(self):
        return ()

    def storage_key(self):
        return "legacy"

    def describe(self):
        return self.name or "参考图里的角色"
