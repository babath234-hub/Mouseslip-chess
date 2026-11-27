importScripts("https://www.gstatic.com/firebasejs/10.14.1/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/10.14.1/firebase-messaging-compat.js");

firebase.initializeApp({
    apiKey: "AIzaSyB2LpWJEMfIRtb45InBjZvLcH4gW-eFZiY",
    authDomain: "mouseslip-chess.firebaseapp.com",
    projectId: "mouseslip-chess",
    storageBucket: "mouseslip-chess.firebasestorage.app",
    messagingSenderId: "858877919720",
    appId: "1:858877919720:web:c76c48d5e4033cc250e45d",
    measurementId: "G-WYPRKP698X"
});

const messaging = firebase.messaging();

messaging.onBackgroundMessage(payload => {
    if (payload.notification) return;
    const title = payload.data?.title || "Mouseslip Chess";
    const options = {
        body: payload.data?.body || "You have a tournament update.",
        icon: "/favicon.ico",
        data: { url: payload.data?.url || "/" }
    };
    self.registration.showNotification(title, options);
});

self.addEventListener("notificationclick", event => {
    event.notification.close();
    const target = event.notification.data?.url || "/";
    event.waitUntil(clients.openWindow(target));
});
