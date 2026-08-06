/**
 * 演示模式开关。
 * - npm run demo（vite --mode demo）读取 .env.demo：VITE_DEMO_MODE=true
 * - 普通 npm run dev / npm run build 不读取 .env.demo，恒为 false
 */
export const DEMO_MODE: boolean = import.meta.env.VITE_DEMO_MODE === "true";
