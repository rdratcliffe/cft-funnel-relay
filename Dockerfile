FROM python:3.12-slim
COPY app.py /app.py
EXPOSE 8080
CMD ["python","/app.py"]
