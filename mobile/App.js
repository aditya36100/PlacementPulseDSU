import { useEffect, useRef, useState } from 'react';
import { BackHandler, Linking, Platform, StatusBar, View } from 'react-native';
import { WebView } from 'react-native-webview';
import * as Notifications from 'expo-notifications';
import * as Device from 'expo-device';
import Constants from 'expo-constants';

const APP_URL = 'https://YOUR-HOSTED-WEB-URL'; // the hosted web/ folder

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true, shouldShowBanner: true, shouldShowList: true,
    shouldPlaySound: true, shouldSetBadge: false,
  }),
});

async function getPushToken() {
  if (!Device.isDevice) return null;
  if (Platform.OS === 'android') {
    await Notifications.setNotificationChannelAsync('placements', {
      name: 'Placement updates',
      importance: Notifications.AndroidImportance.HIGH,
      vibrationPattern: [0, 250, 250, 250],
    });
  }
  let { status } = await Notifications.getPermissionsAsync();
  if (status !== 'granted') ({ status } = await Notifications.requestPermissionsAsync());
  if (status !== 'granted') return null;
  const projectId = Constants.expoConfig?.extra?.eas?.projectId ?? Constants.easConfig?.projectId;
  return (await Notifications.getExpoPushTokenAsync({ projectId })).data;
}

export default function App() {
  const web = useRef(null);
  const [token, setToken] = useState(null);
  const canBack = useRef(false);
  const host = new URL(APP_URL).host;

  useEffect(() => { getPushToken().then(setToken).catch(() => {}); }, []);

  useEffect(() => {
    const sub = BackHandler.addEventListener('hardwareBackPress', () => {
      if (canBack.current) { web.current?.goBack(); return true; }
      return false;
    });
    return () => sub.remove();
  }, []);

  const inject = () => {
    if (!token) return;
    web.current?.injectJavaScript(
      `window.__PUSH_TOKEN=${JSON.stringify(token)};window.dispatchEvent(new Event('pushtoken'));true;`
    );
  };
  useEffect(inject, [token]);

  const openExternal = (url) => { Linking.openURL(url).catch(() => {}); return false; };

  return (
    <View style={{ flex: 1, backgroundColor: '#13152E', paddingTop: StatusBar.currentHeight || 0 }}>
      <StatusBar barStyle="light-content" backgroundColor="#13152E" />
      <WebView
        ref={web}
        source={{ uri: APP_URL }}
        javaScriptEnabled
        domStorageEnabled
        onLoadEnd={inject}
        onNavigationStateChange={(s) => { canBack.current = s.canGoBack; }}
        onShouldStartLoadWithRequest={(r) => {
          try { return new URL(r.url).host === host || r.url.startsWith('about:') ? true : openExternal(r.url); }
          catch { return true; }
        }}
        onOpenWindow={(e) => openExternal(e.nativeEvent.targetUrl)}
      />
    </View>
  );
}
