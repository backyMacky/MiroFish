import axios from 'axios'
import i18n from '../i18n'
import { apiRoot } from './urls'

// 创建axios实例
// API modules already include /api/. Prefix only the plugin mount directory.
const service = axios.create({
  baseURL: apiRoot(import.meta.env.VITE_API_BASE_URL, import.meta.env.BASE_URL),
  timeout: 300000, // 5分钟超时（本体生成可能需要较长时间）
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
service.interceptors.request.use(
  config => {
    config.headers['Accept-Language'] = i18n.global.locale.value
    return config
  },
  error => {
    if (!axios.isCancel(error)) console.error('Request failed', { code: error.code })
    return Promise.reject(error)
  }
)

// 响应拦截器（容错重试机制）
service.interceptors.response.use(
  response => {
    const res = response.data
    
    // 如果返回的状态码不是success，则抛出错误
    if (!res.success && res.success !== undefined) {
      console.error('API reported failure')
      return Promise.reject(new Error(res.error || res.message || 'Error'))
    }
    
    return res
  },
  error => {
    if (axios.isCancel(error)) return Promise.reject(error)
    // Axios errors contain headers, request payloads and provider response bodies.
    console.error('API request failed', { code: error.code, status: error.response?.status })
    const apiError = error.response?.data?.error || error.response?.data?.message
    
    // 处理超时
    if (error.code === 'ECONNABORTED' && error.message.includes('timeout')) {
      console.error('Request timeout')
    }
    
    // 处理网络错误
    if (error.message === 'Network Error') {
      console.error('Network error - please check your connection')
    }

    // Axios rejects non-2xx responses before the success interceptor can
    // surface the backend's safe, actionable error message.
    if (typeof apiError === 'string' && apiError) {
      error.message = apiError
    }
    
    return Promise.reject(error)
  }
)

export default service
