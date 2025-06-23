# Use official Python image
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    libportaudio2 \
    libportaudiocpp0 \
    ffmpeg \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Expose the port your app runs on (e.g., for Streamlit or Flask)
EXPOSE 7860

# Command to run your app (change according to your app type)
# Streamlit example:
CMD ["streamlit", "run", "main.py", "--server.port=7860", "--server.address=0.0.0.0"]

# Flask example (uncomment if using Flask):
# CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:7860", "app:app"]
