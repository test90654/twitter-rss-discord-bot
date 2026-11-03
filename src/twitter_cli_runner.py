# twitter-cli を、固定ポスト（プロフィールのピン留め）も読めるようにして実行する。
#
# X の UserTweets は固定ポストを {"type": "TimelinePinEntry", "entry": {...}} という
# 単数形の "entry" で返すが、twitter-cli 0.8.5 のパーサーは "entries" / "moduleItems" しか見ないため、
# user-posts の結果から固定ポストが抜け落ちる。ここで "entry" を "entries" に読み替えてから CLI を動かす。
#
# 使い方は twitter-cli と同じ: python src/twitter_cli_runner.py user-posts amagi_amuse --max 100 --json
import twitter_cli.client as _client
from twitter_cli.parser import parse_timeline_response as _orig_parse


def _with_pinned(get_instructions):
    def wrapped(data):
        instructions = get_instructions(data)
        if not isinstance(instructions, list):
            return instructions
        fixed = []
        for ins in instructions:
            if isinstance(ins, dict) and not ins.get("entries") and isinstance(ins.get("entry"), dict):
                ins = {**ins, "entries": [ins["entry"]]}
            fixed.append(ins)
        return fixed
    return wrapped


def parse_timeline_response(data, get_instructions):
    return _orig_parse(data, _with_pinned(get_instructions))


_client.parse_timeline_response = parse_timeline_response

if __name__ == "__main__":
    from twitter_cli.cli import cli
    cli()
