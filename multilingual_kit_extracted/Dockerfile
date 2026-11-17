FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-tam tesseract-ocr-eng pandoc antiword fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/extractor
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py profiles.json ./
ENTRYPOINT ["python", "extract.py"]
