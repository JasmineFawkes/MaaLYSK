import { latestReleaseMeta } from "./version";
import { getNavItems, getNoticeItems } from "./sidebar";

const versionBadgeHtml = `<span class="nav-version-badge"><span class="nav-version-spark">✦</span><span>${latestReleaseMeta.version}</span></span>`;

const versionNavItem = {
    text: versionBadgeHtml,
    link: "#version-popup",
    activeMatch: "^$",
};

export const zhNav = [
    versionNavItem,
    {
        text: '<i class="ri-megaphone-line"></i> 公告栏',
        items: [
            ...getNoticeItems(),
            { text: '更新公告', link: '#maaly-popup-__release__' },
            ...getNavItems("zh_cn", "announcement"),
        ],
    },
    {
        text: '<i class="ri-book-read-line"></i> 用户手册',
        items: getNavItems("zh_cn", "manual"),
    },
    {
        text: '<i class="ri-code-s-slash-line"></i> 开发文档',
        items: getNavItems("zh_cn", "develop"),
    },
    {
        text: '<i class="ri-link"></i> 友情链接',
        items: [
            { text: "MAA", link: "https://maa.plus/" },
            { text: "Mirror酱", link: "https://mirrorchyan.com/zh/projects?source=MaaLYSKtop&rid=MaaLYSK" },
            { text: "MaaYuan", link: "https://maayuan.com/" },
            { text: "恋与深空bwiki", link: "https://lysk.site" },
        ],
    },
];

export const enNav = [
    versionNavItem,
    {
        text: '<i class="ri-megaphone-line"></i> Bulletin',
        items: getNavItems("en_us", "announcement"),
    },
    {
        text: '<i class="ri-book-read-line"></i> User Manual',
        items: getNavItems("en_us", "manual"),
    },
    {
        text: '<i class="ri-code-s-slash-line"></i> Developer Guide',
        items: getNavItems("en_us", "develop"),
    },
    {
        text: "Links",
        items: [
            { text: "MAA", link: "https://maa.plus/" },
            { text: "Mirror酱", link: "https://mirrorchyan.com/zh/projects?source=MaaLYSKtop&rid=MaaLYSK" },
            { text: "MaaYuan", link: "https://maayuan.com/" },     
            { text: "恋与深空bwiki", link: "https://lysk.site" },
        ],
    },
];
