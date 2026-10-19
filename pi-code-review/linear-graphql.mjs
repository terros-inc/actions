// Send one GraphQL request to the fixed-destination host proxy. The Linear API
// key stays on the host; this helper reuses the independent session token that
// Pi already has for model requests.
import { readFile } from 'node:fs/promises';

const MAX_REQUEST_BYTES = 256 * 1024;
let input = '';
try {
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > MAX_REQUEST_BYTES) {
      throw new Error('GraphQL request exceeds size limit');
    }
  }
  const request = JSON.parse(input);
  if (!request || typeof request.query !== 'string' || !request.query.trim()) {
    throw new Error('GraphQL request needs a query document');
  }

  const access = JSON.parse(await readFile('/review/context/linear.json', 'utf8'));
  const models = JSON.parse(await readFile('/review/context/models.json', 'utf8'));
  const token = models.providers['review-proxy'].apiKey;
  const response = await fetch(`${access.baseUrl}/linear/graphql`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
    redirect: 'error',
    signal: AbortSignal.timeout(35000),
  });
  const result = await response.json();
  if (!response.ok || (Array.isArray(result.errors) && result.errors.length) || result.error) {
    process.stderr.write(`${JSON.stringify({ httpStatus: response.status, errors: result.errors, error: result.error })}\n`);
    process.exit(1);
  }
  process.stdout.write(`${JSON.stringify(result)}\n`);
} catch (error) {
  const expected = error.message === 'GraphQL request exceeds size limit'
    || error.message === 'GraphQL request needs a query document';
  process.stderr.write(expected ? `${error.message}\n` : 'Linear GraphQL request unavailable\n');
  process.exit(1);
}
