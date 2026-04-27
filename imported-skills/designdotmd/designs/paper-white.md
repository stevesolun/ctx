---
version: alpha
name: Paper White
description: The e-reader palette: ink, cream, nothing else.
colors:
  primary: "#1B1A17"
  secondary: "#5C5A54"
  tertiary: "#9C3B1B"
  neutral: "#F5EFE1"
  surface: "#FAF5E8"
  on-primary: "#FAF5E8"
typography:
  display:
    fontFamily: Source Serif 4
    fontSize: 4.5rem
    fontWeight: 500
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Source Serif 4
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: Source Serif 4
    fontSize: 1.1rem
    lineHeight: 1.75
  label:
    fontFamily: Source Sans 3
    fontSize: 0.75rem
    letterSpacing: "0.1em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

Purpose-built for long-form reading. A paper-cream background, warm black ink, a rust signal. No chrome, no noise.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1B1A17`):** Headlines and core text.
- **Secondary (`#5C5A54`):** Borders, captions, and metadata.
- **Tertiary (`#9C3B1B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5EFE1`):** The page foundation.

## Typography

- **display:** Source Serif 4 4.5rem
- **h1:** Source Serif 4 2.5rem
- **body:** Source Serif 4 1.1rem
- **label:** Source Sans 3 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
