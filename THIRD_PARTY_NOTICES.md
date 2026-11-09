# Composants tiers

Ce fichier recense les composants tiers embarqués dans le dépôt et reproduit
leurs licences. Il complète [`LICENSE`](LICENSE), qui couvre le code original
de la Plateforme Foy'z & Bar.

Les dépendances Python déclarées dans `requirements.txt` et
`requirements-dev.txt` conservent leurs propres licences ; elles ne sont pas
reproduites ici.

## Composants embarqués

Versions vérifiées le 9 novembre 2026.

| Composant | Version | Fichiers | Licence |
|---|---|---|---|
| [Bootstrap](https://github.com/twbs/bootstrap/tree/v5.3.3) | 5.3.3 | `app/static/vendor/bootstrap/bootstrap.min.css`, `app/static/vendor/bootstrap/bootstrap.bundle.min.js` | MIT |
| [Bootstrap Icons](https://github.com/twbs/icons/tree/v1.11.3) | 1.11.3 | `app/static/vendor/bootstrap-icons/bootstrap-icons.min.css`, `app/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff`, `app/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2` | MIT |
| [Chart.js](https://github.com/chartjs/Chart.js/tree/v4.4.3) | 4.4.3 | `app/static/vendor/chart/chart.umd.min.js` | MIT |

Les fichiers minifiés conservent également leur bandeau de copyright d'origine.

## Licence MIT

Bootstrap, Bootstrap Icons et Chart.js sont distribués sous la licence MIT.
Le texte complet ci-dessous s'applique aux trois composants ; les avis de
copyright correspondants y sont regroupés.

```text
The MIT License (MIT)

Copyright (c) 2011-2024 The Bootstrap Authors
Copyright (c) 2019-2024 The Bootstrap Authors
Copyright (c) 2014-2024 Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
