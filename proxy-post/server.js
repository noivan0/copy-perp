const corsAnywhere = require('cors-anywhere');

const host = '0.0.0.0';
const port = process.env.PORT || 8080;

corsAnywhere.createServer({
  originWhitelist: [],  // 모든 Origin 허용
  requireHeader: [],
  removeHeaders: ['cookie', 'cookie2'],
  httpProxyOptions: {
    xfwd: false,
  },
}).listen(port, host, () => {
  console.log(`Pacifica POST Proxy running on ${host}:${port}`);
});
