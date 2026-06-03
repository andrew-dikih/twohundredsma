# Use the official Python image as a base image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file to the container
COPY requirements.txt .

# Install any dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Use ptvsd or debugpy for debugging
RUN pip install debugpy

# Verify uvicorn installation
RUN uvicorn --version || echo "Uvicorn installation failed"

# Copy the rest of the application files to the container
COPY . .

# Make sure the local SQLite fallback dir exists; on Azure the app uses Cosmos
# (COSMOS_CONNECTION_STRING set) and never writes to disk.
RUN mkdir -p /data /app/advisor_data

# Expose the port FastAPI will run on
EXPOSE 8000 8001

# Default to the advisor on 8001 so the image runs cleanly in Azure Container
# Apps with no command override. docker-compose overrides this locally to add
# --reload.
CMD ["uvicorn", "advisor.app:app", "--host", "0.0.0.0", "--port", "8001"]
