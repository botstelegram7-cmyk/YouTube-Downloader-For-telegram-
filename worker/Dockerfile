FROM node:22-bookworm-slim
ENV NODE_ENV=production
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip ffmpeg ca-certificates git && rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages --no-cache-dir yt-dlp
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY server.js ./
ENV PORT=8080
EXPOSE 8080
CMD ["npm", "start"]
