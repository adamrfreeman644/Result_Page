FROM python:3.13-alpine
WORKDIR /app
COPY VERSION ./
COPY server/ server/
COPY dist/ dist/
EXPOSE 6544
CMD ["python", "server/multi_event.py"]
