FROM node:24-bookworm-slim
WORKDIR /app
RUN chown node:node /app
COPY --chown=node:node package.json package-lock.json ./
USER node
RUN npm ci --no-audit --no-fund
COPY --chown=node:node . .
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
