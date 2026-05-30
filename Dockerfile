FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

ENV PORT=7860
ENV FLASK_DEBUG=false

CMD ["gunicorn", "app:app", "--workers", "1", "--timeout", "120", "--bind", "0.0.0.0:7860"]
