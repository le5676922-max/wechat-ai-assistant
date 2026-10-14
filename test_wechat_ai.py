import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from wechat_ai import Assistant, ChatModel, Config, WeChatUI, build_instructions, extract_style_examples, image_data_url, load_config, normalize_messages


class FakeModel:
    def __init__(self, output="收到，你先歇会儿"):
        self.output = output
        self.calls = []

    def generate(self, model, instructions, context):
        self.calls.append((model, instructions, context))
        return self.output

    def describe_image(self, model, image_bytes):
        self.calls.append((model, "vision", image_bytes))
        return "A solid red square."


class FakeWeChat:
    def __init__(self):
        self.sent = []

    def send(self, friend, message):
        self.sent.append((friend, message))

def sample_config():
    return Config(("小明",), "deepseek-v4-flash", "自然简短", 10, 3, 5, 120, "（AI助手代回）", "deepseek-flash")


class AssistantTests(unittest.TestCase):
    def test_config_requires_explicit_friend(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"allowed_friends": ["请改成好友的微信备注"]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)
            path.write_text(json.dumps({"allowed_friends": ["小明"], "model": "qwen-mt-uni"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_filters_self_group_and_nontext(self):
        rows = [
            {"消息发送人": "自己", "消息内容": "你好", "消息类型": "文本"},
            {"消息发送人": "小明", "消息内容": "[图片]", "消息类型": "图片"},
            {"消息发送人": "其他群员", "消息内容": "在吗", "消息类型": "文本"},
            {"消息发送人": "小明", "消息内容": "今天好累", "消息类型": "文本"},
        ]
        self.assertEqual(normalize_messages("小明", rows, 5), ["今天好累"])
        self.assertEqual(WeChatUI._rows({"文本内容": ["[图片]", "你好"], "消息发送人": ["小明", "小明"]}), [
            {"消息发送人": "小明", "消息内容": "你好", "消息类型": "文本"}
        ])
        self.assertEqual(WeChatUI._rows({"文本内容": ["图片"], "消息发送人": ["小明"], "图片数量": 1}), [
            {"消息发送人": "小明", "消息内容": "[图片]", "消息类型": "图片"}
        ])

    def test_preview_does_not_send_and_uses_skill(self):
        config = sample_config()
        model = FakeModel()
        wechat = FakeWeChat()
        assistant = Assistant(config, model, wechat, False)
        result = assistant.process("小明", [{"消息发送人": "小明", "消息内容": "今天好累", "消息类型": "文本"}])
        self.assertEqual(result, "收到，你先歇会儿（AI助手代回）")
        self.assertEqual(wechat.sent, [])
        self.assertIn("先接住情绪", build_instructions(config))
        self.assertEqual(model.calls[0][0], "deepseek-v4-flash")
        self.assertIn("今天好累", model.calls[0][2][-1]["content"])

    def test_send_only_whitelisted_and_reply_to_every_message(self):
        config = sample_config()
        model = FakeModel()
        wechat = FakeWeChat()
        assistant = Assistant(config, model, wechat, True)
        rows = [{"消息发送人": "小明", "消息内容": "你好", "消息类型": "文本"}]
        self.assertIsNone(assistant.process("陌生人", rows))
        assistant.process("小明", rows)
        self.assertEqual(wechat.sent, [("小明", "收到，你先歇会儿（AI助手代回）")])
        assistant.process("小明", rows)
        model.output = "这个我得自己看一下"
        assistant.process("小明", [{"消息发送人": "小明", "消息内容": "验证码是多少", "消息类型": "文本"}])
        model.output = "好啊，你想什么时候"
        assistant.process("小明", [{"消息发送人": "小明", "消息内容": "一起出去玩吗", "消息类型": "文本"}])
        self.assertEqual(len(wechat.sent), 4)
        self.assertIn("明确答应", build_instructions(config))

    def test_image_reply_has_multimodal_payload(self):
        model = FakeModel("这张图看起来是红色的")
        assistant = Assistant(sample_config(), model, FakeWeChat(), False)
        image = Image.new("RGB", (8, 8), "red")
        output = io.BytesIO()
        image.save(output, format="PNG")
        result = assistant.process("小明", [{"消息发送人": "小明", "消息内容": "[图片]", "消息类型": "图片", "图片数据": output.getvalue()}])
        self.assertIn("红色", result)
        self.assertEqual(model.calls[0][0], "deepseek-flash")
        self.assertEqual(model.calls[0][1], "vision")
        self.assertEqual(model.calls[1][0], "deepseek-v4-flash")
        self.assertIn("A solid red square", model.calls[1][2][-1]["content"])

    def test_visible_bubble_side_filters_own_message(self):
        class Item:
            def __init__(self, text, kind, side):
                self.text, self.kind = text, kind
                self.image = Image.new("RGB", (300, 90), "white")
                box = (15, 15, 110, 75) if side == "left" else (190, 15, 285, 75)
                ImageDraw.Draw(self.image).rectangle(box, fill="lightgray" if side == "left" else "lightgreen")
            def class_name(self):
                return self.kind
            def window_text(self):
                return self.text
            def capture_as_image(self):
                return self.image
        incoming = WeChatUI._item_row(Item("你好", "mmui::ChatTextItemView", "left"), "小明")
        outgoing = WeChatUI._item_row(Item("你好", "mmui::ChatTextItemView", "right"), "小明")
        picture = WeChatUI._item_row(Item("图片", "mmui::ChatBubbleReferItemView", "left"), "小明")
        sticker = WeChatUI._item_row(Item("动画表情", "mmui::ChatBubbleReferItemView", "left"), "小明")
        self.assertEqual(incoming["消息内容"], "你好")
        self.assertIsNone(outgoing)
        self.assertIsInstance(picture["图片数据"], bytes)
        self.assertIn("动画表情", sticker["消息内容"])
        self.assertEqual(WeChatUI._classify_item(Item("你好", "mmui::ChatTextItemView", "right"), "小明")[0], "done")
        long_text = Item("很长的消息", "mmui::ChatTextItemView", "left")
        ImageDraw.Draw(long_text.image).rectangle((65, 5, 240, 85), fill="lightgray")
        self.assertIsNotNone(WeChatUI._item_row(long_text, "小明"))
        obscured = Item("暂时被遮挡", "mmui::ChatTextItemView", "left")
        obscured.image = Image.new("RGB", (300, 90), "white")
        self.assertEqual(WeChatUI._classify_item(obscured, "小明")[0], "retry")

    def test_only_my_chat_messages_become_style(self):
        records = [
            {"消息发送人": "平安", "消息类型": "文本", "消息内容": "累死了"},
            {"消息发送人": "用户本人", "消息类型": "文本", "消息内容": "笑死了"},
            {"消息发送人": "用户本人", "消息类型": "文本", "消息内容": "验证码123456"},
            {"消息发送人": "用户本人", "消息类型": "图片", "消息内容": "[图片]"},
        ]
        self.assertEqual(extract_style_examples(records, "平安"), ["笑死了"])

    def test_image_size_limit(self):
        with self.assertRaises(ValueError):
            image_data_url(b"x" * (32 * 1024 * 1024 + 1))

    def test_chat_request_shape(self):
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="你好"))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = ChatModel(client).generate("deepseek-v4-flash", "回复规则", [{"role": "user", "content": "在吗"}])
        self.assertEqual(result, "你好")
        self.assertEqual(calls[0]["model"], "deepseek-v4-flash")
        self.assertEqual(calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(calls[0]["messages"], [
            {"role": "system", "content": "回复规则"},
            {"role": "user", "content": "在吗"},
        ])

    def test_vision_request_shape(self):
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="A red square."))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        picture = Image.new("RGB", (8, 8), "red")
        output = io.BytesIO()
        picture.save(output, format="PNG")
        self.assertEqual(ChatModel(client).describe_image("deepseek-flash", output.getvalue()), "A red square.")
        parts = calls[0]["messages"][0]["content"]
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()
