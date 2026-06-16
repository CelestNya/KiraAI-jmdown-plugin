import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'JMDown Plugin',
  description: 'KiraAI JMComic Downloader Plugin 开发者文档',
  lang: 'zh-CN',
  base: '/KiraAI-jmdown-plugin/',
  themeConfig: {
    nav: [
      { text: '首页', link: '/' },
      { text: 'GitHub', link: 'https://github.com/CelestNya/KiraAI-jmdown-plugin' },
    ],
    sidebar: [
      {
        text: '指南',
        items: [
          { text: '简介', link: '/' },
          { text: '架构总览', link: '/architecture' },
          { text: '插件系统集成', link: '/plugin-system' },
          { text: '后台任务系统', link: '/background-tasks' },
          { text: 'NapCat Stream 上传', link: '/napcat-stream' },
          { text: '配置参考', link: '/config-reference' },
          { text: '开发指南', link: '/development' },
        ],
      },
    ],
    socialLinks: [
      { icon: 'github', link: 'https://github.com/CelestNya/KiraAI-jmdown-plugin' },
    ],
    search: {
      provider: 'local',
    },
  },
})
