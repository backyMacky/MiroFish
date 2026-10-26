import assert from 'node:assert/strict'
import test from 'node:test'
import axios from 'axios'
import { apiRoot } from '../src/api/urls.js'

for (const [configured, mount, expected] of [
  [undefined, '/', '/api/graph/ontology/generate'],
  ['', '/p/mirofish/', '/p/mirofish/api/graph/ontology/generate'],
  [undefined, '/p/mirofish/', '/p/mirofish/api/graph/ontology/generate'],
  ['/p/mirofish/api/', '/', '/p/mirofish/api/graph/ontology/generate'],
  ['/p/mirofish/', '/', '/p/mirofish/api/graph/ontology/generate'],
  ['/api', '/', '/api/graph/ontology/generate'],
  ['http://localhost:5001/api/', '/', 'http://localhost:5001/api/graph/ontology/generate'],
  ['http://localhost:5001/', '/', 'http://localhost:5001/api/graph/ontology/generate'],
]) {
  test(`Axios joins the API path once for ${configured ?? mount}`, () => {
    const client = axios.create({ baseURL: apiRoot(configured, mount) })
    assert.equal(client.getUri({ url: '/api/graph/ontology/generate' }), expected)
    assert.equal(client.getUri({ url: '/api/simulation/history', params: { limit: 20 } }),
      expected.replace('graph/ontology/generate', 'simulation/history?limit=20'))
  })
}
