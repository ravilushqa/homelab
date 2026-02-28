FROM node:20-alpine

RUN addgroup -S appgroup && adduser -S appuser -G appgroup

WORKDIR /app

COPY --chown=appuser:appgroup package.json .
RUN npm install --production

COPY --chown=appuser:appgroup *.js .

USER appuser

EXPOSE 3000
CMD ["node", "server.js"]
