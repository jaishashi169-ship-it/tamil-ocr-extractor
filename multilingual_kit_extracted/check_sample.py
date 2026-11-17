"""Run against the original PDF. Title and list reference transcribed from page 1."""
import sys
from extract import extract

books = """திருக்குறள்
நாலடியார்
நான்மணிக்கடிகை
இன்னா நாற்பது
இனியவை நாற்பது
திரிகடுகம்
ஆசாரக்கோவை
பழமொழி நானூறு
சிறுபஞ்சமூலம்
ஏலாதி
முதுமொழிக்காஞ்சி""".splitlines()
pages = extract(sys.argv[1])
assert len(pages) == 13
lines = pages[0]["text"].splitlines()
assert lines[0] == "பதினெண் கீழ்க்கணக்கு நூல்கள்"
start = lines.index("நீதி நூல்கள்") + 1
assert lines[start:start+11] == [f"{i}. {book}" for i,book in enumerate(books,1)]
assert all(p["text"].splitlines()[-1] == str(i) for i,p in enumerate(pages,2))
print("Passed: title, eleven numbered entries, all thirteen printed page numbers.")
