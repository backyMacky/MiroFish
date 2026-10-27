<template>
  <router-view />
</template>

<script setup>
// 使用 Vue Router 来管理页面
</script>

<style>
/* 全局样式重置 */
* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

#app {
  font-family: 'JetBrains Mono', 'Space Grotesk', 'Noto Sans SC', monospace;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  color: #000000;
  background-color: #ffffff;
}

/* 滚动条样式 */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
}

::-webkit-scrollbar-thumb {
  background: #000000;
}

::-webkit-scrollbar-thumb:hover {
  background: #333333;
}

/* 全局按钮样式 */
button {
  font-family: inherit;
}

/* The embedded workspace narrows when the host opens its import drawer. */
@media (max-width: 1100px) {
  .main-view .app-header {
    height: auto;
    min-height: 60px;
    flex-shrink: 0;
    flex-wrap: wrap;
    gap: 12px;
    padding: 12px 16px;
  }

  .main-view .app-header .header-center {
    position: static;
    transform: none;
    margin-inline: auto;
  }

  .main-view .app-header .header-right {
    flex-wrap: wrap;
    gap: 8px;
  }
}
</style>
