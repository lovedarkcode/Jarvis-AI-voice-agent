"""Convert presentation formatting to plain text before any speech provider."""
import html
import re


def spoken_text(text):
    text = re.sub(r'```[^\n]*\n.*?```', ' Code omitted. ', text, flags=re.S)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)', '', text, flags=re.M)
    text = text.replace('*', '').replace('`', '')
    text = re.sub(r'(?<!\w)_+|_+(?!\w)', '', text)
    return re.sub(r'\s+', ' ', text).strip()
