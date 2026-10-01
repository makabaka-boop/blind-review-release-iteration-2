FROM node:20-alpine AS build

WORKDIR /build

COPY package.json package-lock.json* ./
RUN npm install

COPY index.html vite.config.js ./
COPY src ./src
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
